"""
Ticket allocation with distributed seat locking and race-condition-safe checkout.

Author: Akshay Prajapati (@Akshay171124)

History
-------
v1  – Original implementation used a 30-second TTL on seat locks.  Under load,
      users would lose their selected seats mid-checkout because the payment
      gateway round-trip regularly exceeded 30 s.

v2  – Bumped lock TTL to 5 minutes and introduced a `pending_payment` reservation
      state so the seat stays held while the payment intent is being confirmed.
      Fix designed jointly with Atharva Patil (@Atharva7781) who identified the
      race window in the payment callback handler (see services/payments/
      checkout_service.py).  The pending_payment state bridges the gap between
      seat selection and Stripe webhook confirmation.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.ticket import Ticket, TicketStatus
from models.event import Event
from core.exceptions import ConflictError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

# --- Lock configuration ---
# Originally 30 s; raised to 5 min after the checkout race condition was
# discovered during load testing (see module docstring).
SEAT_LOCK_TTL_SECONDS: int = 300  # 5 minutes

# How long a pending_payment reservation can sit before it is considered
# abandoned and eligible for cleanup.
PENDING_PAYMENT_EXPIRY_SECONDS: int = 900  # 15 minutes

LOCK_KEY_PREFIX = "seat_lock:"


class ReservationState(str, Enum):
    LOCKED = "locked"
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    RELEASED = "released"


class TicketAllocator:
    """
    Manages seat selection, locking, and allocation for an event.

    Flow:
        1. User selects seats -> lock_seats() acquires Redis locks (TTL 5 min).
        2. User proceeds to checkout -> transition_to_pending_payment() moves
           the reservation to `pending_payment` so the seat isn't released
           while the payment gateway processes the charge.
        3. Payment webhook confirms -> confirm_reservation() marks tickets as
           CONFIRMED in the database and releases the Redis locks.
        4. If no confirmation arrives within PENDING_PAYMENT_EXPIRY_SECONDS,
           the cleanup job releases the seats.
    """

    def __init__(self, db: AsyncSession, redis: aioredis.Redis) -> None:
        self._db = db
        self._redis = redis

    # ------------------------------------------------------------------
    # Seat locking (Redis)
    # ------------------------------------------------------------------

    def _lock_key(self, event_id: UUID, seat_id: str) -> str:
        return f"{LOCK_KEY_PREFIX}{event_id}:{seat_id}"

    async def lock_seats(
        self,
        event_id: UUID,
        seat_ids: list[str],
        user_id: UUID,
    ) -> str:
        """
        Attempt to acquire Redis locks for the requested seats.

        Returns a reservation_id that the caller uses for subsequent
        operations (checkout, confirmation, release).
        """
        if not seat_ids:
            raise ValidationError("Must select at least one seat")

        # Verify event exists and has capacity
        event = await self._db.get(Event, event_id)
        if event is None:
            raise NotFoundError(f"Event {event_id} not found")

        reservation_id = str(uuid4())
        acquired: list[str] = []

        try:
            for seat_id in seat_ids:
                key = self._lock_key(event_id, seat_id)
                # SET NX with TTL — atomic lock acquire
                ok = await self._redis.set(
                    key,
                    f"{user_id}:{reservation_id}",
                    nx=True,
                    ex=SEAT_LOCK_TTL_SECONDS,
                )
                if not ok:
                    # Someone else holds this seat — roll back everything
                    raise ConflictError(f"Seat {seat_id} is currently held by another user")
                acquired.append(key)
        except ConflictError:
            # Release any locks we already grabbed in this attempt
            if acquired:
                await self._redis.delete(*acquired)
            raise

        logger.info(
            "Locked %d seats for user %s (reservation %s, event %s, TTL %ds)",
            len(seat_ids), user_id, reservation_id, event_id, SEAT_LOCK_TTL_SECONDS,
        )
        return reservation_id

    # ------------------------------------------------------------------
    # pending_payment transition
    # ------------------------------------------------------------------

    async def transition_to_pending_payment(
        self,
        event_id: UUID,
        reservation_id: str,
        seat_ids: list[str],
        user_id: UUID,
    ) -> None:
        """
        Move locked seats into `pending_payment` state.

        This was added to close the race condition where the Redis TTL would
        expire during payment processing and another user could grab the same
        seats.  Now the DB row acts as the source of truth once checkout begins.
        -- fix coordinated with @Atharva7781
        """
        for seat_id in seat_ids:
            key = self._lock_key(event_id, seat_id)
            holder = await self._redis.get(key)
            expected = f"{user_id}:{reservation_id}"
            if holder is None or holder.decode() != expected:
                raise ConflictError(
                    f"Lock expired or ownership mismatch for seat {seat_id}"
                )

        # Persist reservation rows so the seat is protected even if Redis
        # keys expire before the webhook arrives.
        for seat_id in seat_ids:
            ticket = Ticket(
                event_id=event_id,
                seat_id=seat_id,
                user_id=user_id,
                reservation_id=reservation_id,
                status=TicketStatus.PENDING_PAYMENT,
                locked_at=datetime.utcnow(),
            )
            self._db.add(ticket)

        await self._db.flush()
        logger.info(
            "Reservation %s transitioned to pending_payment (%d seats)",
            reservation_id, len(seat_ids),
        )

    # ------------------------------------------------------------------
    # Confirmation (called from payment webhook)
    # ------------------------------------------------------------------

    async def confirm_reservation(self, reservation_id: str) -> int:
        """
        Mark all tickets for a reservation as CONFIRMED and release Redis locks.

        Returns the number of tickets confirmed.
        """
        stmt = (
            select(Ticket)
            .where(
                and_(
                    Ticket.reservation_id == reservation_id,
                    Ticket.status == TicketStatus.PENDING_PAYMENT,
                )
            )
        )
        tickets = (await self._db.execute(stmt)).scalars().all()

        if not tickets:
            raise NotFoundError(f"No pending tickets for reservation {reservation_id}")

        keys_to_release: list[str] = []
        for ticket in tickets:
            ticket.status = TicketStatus.CONFIRMED
            ticket.confirmed_at = datetime.utcnow()
            keys_to_release.append(self._lock_key(ticket.event_id, ticket.seat_id))

        await self._db.flush()

        # Release Redis locks (they may have already expired, which is fine)
        if keys_to_release:
            await self._redis.delete(*keys_to_release)

        logger.info("Confirmed %d tickets for reservation %s", len(tickets), reservation_id)
        return len(tickets)

    # ------------------------------------------------------------------
    # Cleanup job for abandoned pending_payment reservations
    # ------------------------------------------------------------------

    async def cleanup_abandoned_reservations(self) -> int:
        """
        Release seats stuck in `pending_payment` longer than the expiry window.

        This should be called by a periodic task (e.g., Celery beat every 5 min).
        It catches cases where the payment webhook never fires — network errors,
        user abandonment, gateway timeouts, etc.
        """
        cutoff = datetime.utcnow() - timedelta(seconds=PENDING_PAYMENT_EXPIRY_SECONDS)

        stmt = (
            select(Ticket)
            .where(
                and_(
                    Ticket.status == TicketStatus.PENDING_PAYMENT,
                    Ticket.locked_at < cutoff,
                )
            )
        )
        stale_tickets = (await self._db.execute(stmt)).scalars().all()

        if not stale_tickets:
            return 0

        keys_to_release: list[str] = []
        for ticket in stale_tickets:
            ticket.status = TicketStatus.RELEASED
            ticket.released_at = datetime.utcnow()
            keys_to_release.append(self._lock_key(ticket.event_id, ticket.seat_id))

        await self._db.flush()

        if keys_to_release:
            await self._redis.delete(*keys_to_release)

        logger.warning(
            "Cleaned up %d abandoned pending_payment reservations (cutoff %s)",
            len(stale_tickets), cutoff.isoformat(),
        )
        return len(stale_tickets)
# Race condition fix applied
# Cleanup job
# Seat map data
# Ticket transfer
