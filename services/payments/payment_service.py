"""
Core payment processing service for EventPulse.

Owner: Atharva Dhumal (@atharvadhumal03)
Last major refactor: March 2026 (post-outage hardening)

This module handles all payment intent creation, confirmation, and status
tracking through the Stripe API. Idempotency is enforced at multiple layers
to prevent duplicate charges.
"""

import hashlib
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

import redis
import stripe
from stripe.error import (
    CardError,
    IdempotencyError,
    RateLimitError,
    StripeError,
)

from config.settings import (
    REDIS_URL,
    STRIPE_API_KEY,
    STRIPE_API_VERSION,
)
from services.payments.exceptions import (
    DuplicatePaymentError,
    PaymentProcessingError,
    PaymentTimeoutError,
)

logger = logging.getLogger(__name__)

stripe.api_key = STRIPE_API_KEY
stripe.api_version = STRIPE_API_VERSION

# TTL for idempotency keys stored in Redis. 24 hours is intentionally generous.
# During the March 2026 outage, we had a cascade where the Stripe gateway timed
# out but charges actually went through. When our retry logic kicked in, we
# created NEW payment intents (different idempotency keys) for the same logical
# purchase, resulting in ~$18K in duplicate charges across 42 users. We now
# cache idempotency keys in Redis so that retries within 24h always reuse the
# same key, even across process restarts.
IDEMPOTENCY_KEY_TTL = 86400  # seconds (24 hours)

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

# Stripe metadata key limit is 500 chars; we truncate event names beyond this.
MAX_METADATA_LENGTH = 500


class PaymentStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentService:
    """Handles payment processing through the Stripe API with idempotency guarantees."""

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self._redis = redis_client or redis.from_url(REDIS_URL, decode_responses=True)

    def _generate_idempotency_key(
        self,
        user_id: str,
        event_id: str,
        amount_cents: int,
        timestamp: Optional[datetime] = None,
    ) -> str:
        """
        Generate a deterministic idempotency key from payment parameters.

        The key is a SHA-256 hash of (user_id, event_id, amount, timestamp_minute).
        We truncate the timestamp to the minute so that rapid retries within the
        same calendar minute always produce the same key.

        KNOWN EDGE CASE (atharvadhumal03, 2026-03-14):
        If a user clicks "Pay" at 11:59:59 and the first attempt fails, a retry
        at 12:00:01 will generate a DIFFERENT idempotency key because the minute
        boundary rolled over. This is an accepted risk because:
          1. The 24h Redis cache (see _get_or_set_idempotency_key) catches most
             duplicates at the logical level before we even compute a new key.
          2. Stripe's own idempotency layer provides a second safety net.
          3. The refund_reconciler daily job (see refund_processor.py) catches
             any duplicates that slip through both layers.
        We discussed fixing this with a 90-second window quantization but decided
        the complexity wasn't worth it given the three-layer safety net.
        """
        ts = timestamp or datetime.now(timezone.utc)
        minute_bucket = ts.strftime("%Y-%m-%dT%H:%M")
        raw = f"{user_id}:{event_id}:{amount_cents}:{minute_bucket}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _get_or_set_idempotency_key(
        self, user_id: str, event_id: str, amount_cents: int
    ) -> str:
        """
        Check Redis for an existing idempotency key for this (user, event, amount)
        tuple. If one exists, reuse it. Otherwise, generate and cache a new one.

        This is the PRIMARY defense against duplicate charges. Even if the minute
        boundary rolls over (see _generate_idempotency_key docstring), this cache
        will return the same key for 24 hours.
        """
        cache_key = f"idem:{user_id}:{event_id}:{amount_cents}"
        existing = self._redis.get(cache_key)
        if existing:
            logger.info(
                "Reusing cached idempotency key for user=%s event=%s",
                user_id,
                event_id,
            )
            return existing

        new_key = self._generate_idempotency_key(user_id, event_id, amount_cents)
        self._redis.setex(cache_key, IDEMPOTENCY_KEY_TTL, new_key)
        return new_key

    def create_payment_intent(
        self,
        user_id: str,
        event_id: str,
        amount_cents: int,
        currency: str = "usd",
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Create a Stripe PaymentIntent with idempotency protection.

        Returns a dict with 'client_secret' for frontend confirmation and
        'payment_intent_id' for server-side tracking.
        """
        if amount_cents <= 0:
            raise ValueError(f"amount_cents must be positive, got {amount_cents}")

        idempotency_key = self._get_or_set_idempotency_key(
            user_id, event_id, amount_cents
        )

        payment_metadata = {
            "user_id": user_id,
            "event_id": event_id,
            "source": "eventpulse",
        }
        if metadata:
            for k, v in metadata.items():
                payment_metadata[k] = str(v)[:MAX_METADATA_LENGTH]

        intent = self._call_stripe_with_retry(
            stripe.PaymentIntent.create,
            amount=amount_cents,
            currency=currency.lower(),
            metadata=payment_metadata,
            idempotency_key=idempotency_key,
        )

        logger.info(
            "Created PaymentIntent %s for user=%s event=%s amount=%d%s",
            intent["id"],
            user_id,
            event_id,
            amount_cents,
            currency.upper(),
        )

        return {
            "payment_intent_id": intent["id"],
            "client_secret": intent["client_secret"],
            "status": intent["status"],
            "amount": intent["amount"],
            "currency": intent["currency"],
        }

    def confirm_payment(self, payment_intent_id: str) -> Dict[str, Any]:
        """
        Server-side confirmation of a PaymentIntent. Typically called after
        the frontend collects payment method details.
        """
        intent = self._call_stripe_with_retry(
            stripe.PaymentIntent.confirm,
            payment_intent_id,
        )
        return {
            "payment_intent_id": intent["id"],
            "status": intent["status"],
        }

    def process_payment(
        self,
        user_id: str,
        event_id: str,
        amount_cents: int,
        payment_method_id: str,
        currency: str = "usd",
    ) -> Dict[str, Any]:
        """
        End-to-end payment processing: create intent, attach method, confirm.

        This is the main entry point for server-side initiated payments (e.g.,
        from the admin dashboard or batch ticket purchases).
        """
        result = self.create_payment_intent(
            user_id=user_id,
            event_id=event_id,
            amount_cents=amount_cents,
            currency=currency,
        )

        intent = self._call_stripe_with_retry(
            stripe.PaymentIntent.confirm,
            result["payment_intent_id"],
            payment_method=payment_method_id,
        )

        return {
            "payment_intent_id": intent["id"],
            "status": intent["status"],
            "amount": intent["amount"],
            "currency": intent["currency"],
        }

    def get_payment_status(self, payment_intent_id: str) -> Dict[str, Any]:
        """Retrieve the current status of a PaymentIntent from Stripe."""
        intent = self._call_stripe_with_retry(
            stripe.PaymentIntent.retrieve,
            payment_intent_id,
        )
        return {
            "payment_intent_id": intent["id"],
            "status": intent["status"],
            "amount": intent["amount"],
            "currency": intent["currency"],
            "created": intent["created"],
        }

    def _call_stripe_with_retry(self, func, *args, **kwargs) -> Any:
        """
        Call a Stripe SDK function with exponential backoff retry logic.

        Retries on RateLimitError and transient StripeErrors. Does NOT retry
        on CardError (user-facing) or IdempotencyError (logic bug).
        """
        last_exception = None
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except RateLimitError as e:
                last_exception = e
                wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "Stripe rate limit hit (attempt %d/%d), backing off %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
            except IdempotencyError:
                # This means we sent a different request body with the same
                # idempotency key. This is a bug in our code, not transient.
                raise
            except CardError:
                # Card was declined — no point retrying.
                raise
            except StripeError as e:
                last_exception = e
                if attempt < MAX_RETRIES - 1:
                    wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                    logger.warning(
                        "Stripe error (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1,
                        MAX_RETRIES,
                        str(e),
                        wait,
                    )
                    time.sleep(wait)

        logger.error(
            "Stripe call failed after %d attempts: %s", MAX_RETRIES, last_exception
        )
        raise PaymentProcessingError(
            f"Payment processing failed after {MAX_RETRIES} attempts"
        ) from last_exception
