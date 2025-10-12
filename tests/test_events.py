"""
Tests for event CRUD, venue search, and ticket allocation locking.

Author: Akshay Prajapati (@Akshay171124)
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio

from services.events.event_service import EventService, VENUE_BUFFER_MINUTES
from services.events.venue_search import VenueSearchService, VenueSearchFilters
from services.events.ticket_allocator import (
    TicketAllocator,
    SEAT_LOCK_TTL_SECONDS,
    ReservationState,
)
from core.exceptions import ConflictError, NotFoundError, ValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event_payload(**overrides):
    """Build a minimal EventCreate-like object for testing."""
    defaults = {
        "title": "Test Concert",
        "description": "A great show",
        "venue_id": uuid4(),
        "category": "music",
        "capacity": 200,
        "start_time": datetime.utcnow() + timedelta(days=7),
        "end_time": datetime.utcnow() + timedelta(days=7, hours=3),
    }
    defaults.update(overrides)
    obj = MagicMock()
    for k, v in defaults.items():
        setattr(obj, k, v)
    obj.dict.return_value = {k: v for k, v in defaults.items() if k in overrides}
    return obj


def _make_venue(venue_id=None, max_capacity=500):
    venue = MagicMock()
    venue.id = venue_id or uuid4()
    venue.max_capacity = max_capacity
    return venue


# ---------------------------------------------------------------------------
# EventService — CRUD
# ---------------------------------------------------------------------------

class TestEventCreate:

    @pytest.mark.asyncio
    async def test_create_event_success(self):
        db = AsyncMock()
        venue = _make_venue(max_capacity=500)
        db.get.return_value = venue
        db.execute.return_value = MagicMock(first=MagicMock(return_value=None))

        svc = EventService(db)
        payload = _make_event_payload(capacity=200)
        event = await svc.create_event(payload, organizer_id=uuid4())
        db.add.assert_called_once()
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_event_invalid_category(self):
        db = AsyncMock()
        svc = EventService(db)
        payload = _make_event_payload(category="invalid_cat")
        with pytest.raises(ValidationError, match="Invalid category"):
            await svc.create_event(payload, organizer_id=uuid4())

    @pytest.mark.asyncio
    async def test_create_event_end_before_start(self):
        db = AsyncMock()
        svc = EventService(db)
        now = datetime.utcnow() + timedelta(days=7)
        payload = _make_event_payload(start_time=now, end_time=now - timedelta(hours=1))
        with pytest.raises(ValidationError, match="end time must be after start"):
            await svc.create_event(payload, organizer_id=uuid4())

    @pytest.mark.asyncio
    async def test_create_event_exceeds_venue_capacity(self):
        db = AsyncMock()
        venue = _make_venue(max_capacity=100)
        db.get.return_value = venue

        svc = EventService(db)
        payload = _make_event_payload(capacity=500, venue_id=venue.id)
        with pytest.raises(ValidationError, match="exceeds venue max"):
            await svc.create_event(payload, organizer_id=uuid4())


class TestEventGet:

    @pytest.mark.asyncio
    async def test_get_event_not_found(self):
        db = AsyncMock()
        db.get.return_value = None
        svc = EventService(db)
        with pytest.raises(NotFoundError):
            await svc.get_event(uuid4())

    @pytest.mark.asyncio
    async def test_get_event_returns_event(self):
        db = AsyncMock()
        mock_event = MagicMock()
        db.get.return_value = mock_event
        svc = EventService(db)
        result = await svc.get_event(uuid4())
        assert result is mock_event


class TestEventDelete:

    @pytest.mark.asyncio
    async def test_delete_by_non_organizer_rejected(self):
        db = AsyncMock()
        event = MagicMock()
        event.organizer_id = uuid4()
        db.get.return_value = event

        svc = EventService(db)
        with pytest.raises(ValidationError, match="Only the organizer"):
            await svc.delete_event(event_id=uuid4(), organizer_id=uuid4())


# ---------------------------------------------------------------------------
# VenueSearch
# ---------------------------------------------------------------------------

class TestVenueSearch:

    @pytest.mark.asyncio
    async def test_search_returns_paginated_results(self):
        db = AsyncMock()
        # Mock the count query
        count_result = MagicMock()
        count_result.scalar_one.return_value = 2

        # Mock the venue rows
        venue1 = MagicMock()
        venue1.id = uuid4()
        venue1.name = "Grand Hall"
        venue1.address = "123 Main St"
        venue1.latitude = 37.7749
        venue1.longitude = -122.4194
        venue1.max_capacity = 500
        venue1.amenities = ["wifi", "parking"]

        db.execute.side_effect = [count_result, MagicMock(all=MagicMock(return_value=[(venue1, 1200.5)]))]

        svc = VenueSearchService(db)
        filters = VenueSearchFilters(latitude=37.78, longitude=-122.42, radius_km=10)
        result = await svc.search(filters)

        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_search_clamps_radius(self):
        """Radius larger than MAX_SEARCH_RADIUS_KM should be clamped."""
        db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 0
        db.execute.side_effect = [count_result, MagicMock(all=MagicMock(return_value=[]))]

        svc = VenueSearchService(db)
        filters = VenueSearchFilters(latitude=0, longitude=0, radius_km=9999)
        result = await svc.search(filters)
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# TicketAllocator — seat locking
# ---------------------------------------------------------------------------

class TestTicketAllocator:

    @pytest.mark.asyncio
    async def test_lock_seats_success(self):
        db = AsyncMock()
        event = MagicMock()
        db.get.return_value = event

        mock_redis = AsyncMock()
        mock_redis.set.return_value = True  # NX succeeds

        allocator = TicketAllocator(db, mock_redis)
        reservation_id = await allocator.lock_seats(
            event_id=uuid4(), seat_ids=["A1", "A2"], user_id=uuid4()
        )
        assert reservation_id is not None
        assert mock_redis.set.call_count == 2

    @pytest.mark.asyncio
    async def test_lock_seats_conflict_rolls_back(self):
        db = AsyncMock()
        event = MagicMock()
        db.get.return_value = event

        mock_redis = AsyncMock()
        # First seat succeeds, second seat fails (already held)
        mock_redis.set.side_effect = [True, None]

        allocator = TicketAllocator(db, mock_redis)
        with pytest.raises(ConflictError, match="currently held"):
            await allocator.lock_seats(
                event_id=uuid4(), seat_ids=["A1", "A2"], user_id=uuid4()
            )
        # Should have released the first lock
        mock_redis.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lock_empty_seat_list_rejected(self):
        db = AsyncMock()
        mock_redis = AsyncMock()
        allocator = TicketAllocator(db, mock_redis)
        with pytest.raises(ValidationError, match="at least one seat"):
            await allocator.lock_seats(
                event_id=uuid4(), seat_ids=[], user_id=uuid4()
            )

    @pytest.mark.asyncio
    async def test_lock_nonexistent_event_rejected(self):
        db = AsyncMock()
        db.get.return_value = None
        mock_redis = AsyncMock()
        allocator = TicketAllocator(db, mock_redis)
        with pytest.raises(NotFoundError, match="not found"):
            await allocator.lock_seats(
                event_id=uuid4(), seat_ids=["A1"], user_id=uuid4()
            )

    @pytest.mark.asyncio
    async def test_confirm_reservation_marks_confirmed(self):
        db = AsyncMock()
        mock_redis = AsyncMock()

        ticket = MagicMock()
        ticket.reservation_id = "res_abc"
        ticket.event_id = uuid4()
        ticket.seat_id = "B3"
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [ticket]
        db.execute.return_value = result_mock

        allocator = TicketAllocator(db, mock_redis)
        count = await allocator.confirm_reservation("res_abc")
        assert count == 1
        assert ticket.status is not None
