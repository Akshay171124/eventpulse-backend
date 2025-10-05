"""
Event CRUD service with venue conflict detection and pagination.

Author: Akshay Prajapati (@Akshay171124)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.event import Event, EventStatus
from models.venue import Venue
from schemas.events import EventCreate, EventUpdate, EventListParams
from core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

logger = logging.getLogger(__name__)

VALID_CATEGORIES = frozenset([
    "music", "conference", "sports", "theater", "comedy",
    "food_drink", "networking", "workshop", "charity", "other",
])

# Minimum gap between consecutive events at the same venue (for teardown/setup)
VENUE_BUFFER_MINUTES = 60


class EventService:
    """Handles event lifecycle operations with venue conflict checking."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_category(self, category: str) -> None:
        if category not in VALID_CATEGORIES:
            raise ValidationError(
                f"Invalid category '{category}'. "
                f"Must be one of: {', '.join(sorted(VALID_CATEGORIES))}"
            )

    def _validate_dates(self, start: datetime, end: datetime) -> None:
        if end <= start:
            raise ValidationError("Event end time must be after start time")
        if start < datetime.utcnow() + timedelta(hours=1):
            raise ValidationError(
                "Event must start at least 1 hour from now"
            )
        if (end - start) > timedelta(days=14):
            raise ValidationError("Event duration cannot exceed 14 days")

    async def _validate_capacity(self, venue_id: UUID, requested: int) -> None:
        venue = await self._db.get(Venue, venue_id)
        if venue is None:
            raise NotFoundError(f"Venue {venue_id} not found")
        if requested > venue.max_capacity:
            raise ValidationError(
                f"Requested capacity ({requested}) exceeds venue max ({venue.max_capacity})"
            )
        if requested < 1:
            raise ValidationError("Capacity must be at least 1")

    async def _check_venue_conflict(
        self,
        venue_id: UUID,
        start: datetime,
        end: datetime,
        exclude_event_id: Optional[UUID] = None,
    ) -> None:
        """Ensure no overlapping events exist at the venue (with buffer)."""
        buffered_start = start - timedelta(minutes=VENUE_BUFFER_MINUTES)
        buffered_end = end + timedelta(minutes=VENUE_BUFFER_MINUTES)

        stmt = (
            select(Event.id, Event.title)
            .where(
                and_(
                    Event.venue_id == venue_id,
                    Event.status != EventStatus.CANCELLED,
                    Event.start_time < buffered_end,
                    Event.end_time > buffered_start,
                )
            )
        )
        if exclude_event_id:
            stmt = stmt.where(Event.id != exclude_event_id)

        result = await self._db.execute(stmt)
        conflict = result.first()
        if conflict:
            raise ConflictError(
                f"Venue is booked by '{conflict.title}' (event {conflict.id}) "
                f"during the requested window (including {VENUE_BUFFER_MINUTES}min buffer)"
            )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_event(self, payload: EventCreate, organizer_id: UUID) -> Event:
        self._validate_category(payload.category)
        self._validate_dates(payload.start_time, payload.end_time)
        await self._validate_capacity(payload.venue_id, payload.capacity)
        await self._check_venue_conflict(
            payload.venue_id, payload.start_time, payload.end_time
        )

        event = Event(
            title=payload.title,
            description=payload.description,
            venue_id=payload.venue_id,
            organizer_id=organizer_id,
            category=payload.category,
            capacity=payload.capacity,
            start_time=payload.start_time,
            end_time=payload.end_time,
            status=EventStatus.DRAFT,
        )
        self._db.add(event)
        try:
            await self._db.flush()
        except IntegrityError:
            await self._db.rollback()
            raise ConflictError("Duplicate event — check title and venue combination")

        logger.info("Created event %s for organizer %s", event.id, organizer_id)
        return event

    async def get_event(self, event_id: UUID) -> Event:
        event = await self._db.get(Event, event_id)
        if event is None:
            raise NotFoundError(f"Event {event_id} not found")
        return event

    async def update_event(
        self, event_id: UUID, payload: EventUpdate, organizer_id: UUID
    ) -> Event:
        event = await self.get_event(event_id)
        if event.organizer_id != organizer_id:
            raise ValidationError("Only the organizer can update this event")

        if payload.start_time or payload.end_time:
            start = payload.start_time or event.start_time
            end = payload.end_time or event.end_time
            self._validate_dates(start, end)
            venue_id = payload.venue_id or event.venue_id
            await self._check_venue_conflict(venue_id, start, end, exclude_event_id=event_id)

        if payload.category:
            self._validate_category(payload.category)
        if payload.capacity:
            venue_id = payload.venue_id or event.venue_id
            await self._validate_capacity(venue_id, payload.capacity)

        update_data = payload.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(event, field, value)

        event.updated_at = datetime.utcnow()
        await self._db.flush()
        logger.info("Updated event %s (fields: %s)", event_id, list(update_data.keys()))
        return event

    async def delete_event(self, event_id: UUID, organizer_id: UUID) -> None:
        event = await self.get_event(event_id)
        if event.organizer_id != organizer_id:
            raise ValidationError("Only the organizer can delete this event")
        # Soft-delete: mark cancelled rather than removing from DB
        event.status = EventStatus.CANCELLED
        event.updated_at = datetime.utcnow()
        await self._db.flush()
        logger.info("Soft-deleted event %s", event_id)

    async def list_events(
        self, params: EventListParams
    ) -> dict[str, Any]:
        """Return paginated event list with total count."""
        base = select(Event).where(Event.status != EventStatus.CANCELLED)

        if params.category:
            base = base.where(Event.category == params.category)
        if params.venue_id:
            base = base.where(Event.venue_id == params.venue_id)
        if params.from_date:
            base = base.where(Event.start_time >= params.from_date)
        if params.to_date:
            base = base.where(Event.start_time <= params.to_date)

        # Total count (without pagination)
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_stmt)).scalar_one()

        # Apply ordering and pagination
        stmt = (
            base
            .order_by(Event.start_time.asc())
            .offset(params.offset)
            .limit(params.limit)
        )
        rows = (await self._db.execute(stmt)).scalars().all()

        return {
            "items": rows,
            "total": total,
            "offset": params.offset,
            "limit": params.limit,
        }
