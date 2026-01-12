"""
PostGIS-backed venue search with geo-radius filtering and ranking.

Author: Akshay Prajapati (@Akshay171124)

Performance notes
-----------------
* The ST_DWithin query uses the GIST spatial index on venues.location,
  so distance filtering is index-assisted and avoids a full table scan.
* We compute ST_Distance only on the rows that survive ST_DWithin — this
  keeps the expensive geodesic math to the candidate set.
* For high-traffic deployments consider a materialized view that
  pre-joins venues with their upcoming event counts to avoid the
  correlated subquery in availability checks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

from geoalchemy2 import Geography
from geoalchemy2.functions import ST_DWithin, ST_Distance, ST_MakePoint
from sqlalchemy import Float, and_, cast, case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.venue import Venue
from models.event import Event, EventStatus

logger = logging.getLogger(__name__)

# Earth radius in metres (WGS-84 mean)
_EARTH_RADIUS_M = 6_371_000

# Hard cap so callers can't ask for a 10 000 km radius
MAX_SEARCH_RADIUS_KM = 200
DEFAULT_SEARCH_RADIUS_KM = 25
DEFAULT_PAGE_SIZE = 20


@dataclass
class VenueSearchFilters:
    """Parameters accepted by the search endpoint."""
    latitude: float
    longitude: float
    radius_km: float = DEFAULT_SEARCH_RADIUS_KM
    min_capacity: Optional[int] = None
    max_capacity: Optional[int] = None
    amenities: list[str] = field(default_factory=list)
    available_from: Optional[datetime] = None
    available_to: Optional[datetime] = None
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass
class VenueSearchResult:
    venue_id: UUID
    name: str
    address: str
    latitude: float
    longitude: float
    max_capacity: int
    amenities: list[str]
    distance_km: float
    is_available: bool


class VenueSearchService:
    """Radius-based venue discovery backed by PostGIS / GeoAlchemy2."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def search(self, filters: VenueSearchFilters) -> dict[str, Any]:
        """
        Return venues within *radius_km* of (lat, lon), ranked by distance.

        Query plan (simplified):
            1. ST_DWithin narrows candidates via the GIST index.
            2. Capacity / amenity filters applied as WHERE clauses.
            3. Availability is checked with a NOT EXISTS correlated subquery
               against the events table (only if date window is provided).
            4. Results sorted by geodesic distance ascending.
        """
        radius_km = min(filters.radius_km, MAX_SEARCH_RADIUS_KM)
        radius_m = radius_km * 1000

        # Reference point as a PostGIS geography
        ref_point = ST_MakePoint(filters.longitude, filters.latitude)
        ref_geog = cast(ref_point, Geography)

        # Distance in metres — only computed on candidates inside the radius
        dist_col = ST_Distance(Venue.location, ref_geog).label("distance_m")

        stmt = (
            select(Venue, dist_col)
            .where(ST_DWithin(Venue.location, ref_geog, radius_m))
        )

        # --- capacity filters ---
        if filters.min_capacity is not None:
            stmt = stmt.where(Venue.max_capacity >= filters.min_capacity)
        if filters.max_capacity is not None:
            stmt = stmt.where(Venue.max_capacity <= filters.max_capacity)

        # --- amenity filter (all requested amenities must be present) ---
        # Venue.amenities is stored as a JSONB array in Postgres
        for amenity in filters.amenities:
            stmt = stmt.where(Venue.amenities.contains([amenity]))

        # --- availability window (optional) ---
        # NOTE: This uses a NOT EXISTS subquery.  For venues with thousands of
        # events the correlated subquery can be slow — see the materialized-view
        # suggestion in the module docstring.
        if filters.available_from and filters.available_to:
            conflict_subq = (
                select(literal(1))
                .where(
                    and_(
                        Event.venue_id == Venue.id,
                        Event.status != EventStatus.CANCELLED,
                        Event.start_time < filters.available_to,
                        Event.end_time > filters.available_from,
                    )
                )
                .correlate(Venue)
                .exists()
            )
            stmt = stmt.where(~conflict_subq)

        # --- ordering & pagination ---
        stmt = stmt.order_by(dist_col.asc())

        # Total count before pagination (needed for UI pager)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self._db.execute(count_stmt)).scalar_one()

        offset = (filters.page - 1) * filters.page_size
        stmt = stmt.offset(offset).limit(filters.page_size)

        rows = (await self._db.execute(stmt)).all()

        results: list[VenueSearchResult] = []
        for venue, distance_m in rows:
            results.append(
                VenueSearchResult(
                    venue_id=venue.id,
                    name=venue.name,
                    address=venue.address,
                    latitude=venue.latitude,
                    longitude=venue.longitude,
                    max_capacity=venue.max_capacity,
                    amenities=venue.amenities or [],
                    distance_km=round(distance_m / 1000, 2),
                    is_available=True,  # already filtered above
                )
            )

        logger.debug(
            "Venue search: lat=%.4f lon=%.4f radius=%skm -> %d results (page %d/%d)",
            filters.latitude,
            filters.longitude,
            radius_km,
            total,
            filters.page,
            max((total + filters.page_size - 1) // filters.page_size, 1),
        )

        return {
            "items": results,
            "total": total,
            "page": filters.page,
            "page_size": filters.page_size,
        }
# Materialized view
# Geography type
# Availability cache
# Geo-fence
