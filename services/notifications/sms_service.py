"""
SMS notification service backed by Twilio.

Owner: Vasudha Jain (@jainvasudha)

Rate limiting: each user is capped at SMS_RATE_LIMIT_PER_USER_PER_HOUR
messages per hour (default 5).  This is enforced via a Redis sliding window
so the limit works correctly across multiple worker processes.

Phone number validation uses a lightweight regex check plus Twilio's Lookup
API for carrier validation on first use.  Validated numbers are cached in
Redis for 30 days to avoid repeated lookups.
"""

import logging
import re
import time
from typing import Optional

import redis
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioRestException

from config.settings import (
    REDIS_URL,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_FROM_NUMBER,
    SMS_RATE_LIMIT_PER_USER_PER_HOUR,
)

logger = logging.getLogger(__name__)

# E.164 pattern — international format with country code, 7-15 digits.
_E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")

# Cache validated phone numbers for 30 days to avoid repeated Twilio Lookup calls.
_PHONE_VALIDATION_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days

# Sliding window key prefix for per-user SMS rate limiting.
_RATE_LIMIT_PREFIX = "sms_rl:"


class SMSService:
    """Sends transactional SMS via Twilio with per-user rate limiting."""

    def __init__(
        self,
        twilio_client: Optional[TwilioClient] = None,
        redis_client: Optional[redis.Redis] = None,
    ):
        self._twilio = twilio_client or TwilioClient(
            TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
        )
        self._redis = redis_client or redis.from_url(REDIS_URL, decode_responses=True)
        self._from_number = TWILIO_FROM_NUMBER

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_ticket_confirmation(
        self, to_number: str, user_id: str, event_title: str, ticket_count: int
    ) -> bool:
        body = (
            f"EventPulse: Your booking is confirmed! "
            f"{ticket_count}x ticket(s) for {event_title}. "
            f"Show this SMS or check your email for your e-ticket."
        )
        return self._send(to_number, user_id, body)

    def send_event_reminder(
        self, to_number: str, user_id: str, event_title: str, event_time: str
    ) -> bool:
        body = (
            f"EventPulse Reminder: {event_title} starts at {event_time}. "
            f"Don't forget to bring your ticket! Enjoy the event."
        )
        return self._send(to_number, user_id, body)

    def send_refund_update(
        self, to_number: str, user_id: str, refund_amount: str, event_title: str
    ) -> bool:
        body = (
            f"EventPulse: Your refund of {refund_amount} for {event_title} "
            f"has been processed. Please allow 5-10 business days for the "
            f"amount to appear on your statement."
        )
        return self._send(to_number, user_id, body)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send(self, to_number: str, user_id: str, body: str) -> bool:
        """Validate, rate-check, and dispatch a single SMS."""
        if not self._validate_phone_number(to_number):
            logger.warning("Invalid phone number rejected: %s (user %s)", to_number, user_id)
            return False

        if not self._check_rate_limit(user_id):
            logger.warning(
                "SMS rate limit exceeded for user %s (%d/hr). Message dropped.",
                user_id,
                SMS_RATE_LIMIT_PER_USER_PER_HOUR,
            )
            return False

        try:
            message = self._twilio.messages.create(
                body=body,
                from_=self._from_number,
                to=to_number,
            )
            logger.info(
                "SMS sent to %s (user %s, sid=%s)", to_number, user_id, message.sid
            )
            return True
        except TwilioRestException as exc:
            logger.error(
                "Twilio error sending SMS to %s (user %s): %s", to_number, user_id, exc
            )
            return False

    def _validate_phone_number(self, phone: str) -> bool:
        """
        Validate a phone number in two stages:
          1. Quick regex check against E.164 format.
          2. (First time only) Twilio Lookup API to verify the number is real.
             The result is cached in Redis for 30 days.
        """
        if not _E164_PATTERN.match(phone):
            return False

        cache_key = f"phone_valid:{phone}"
        cached = self._redis.get(cache_key)
        if cached is not None:
            return cached == "1"

        # Call Twilio Lookup API for carrier validation
        try:
            lookup = self._twilio.lookups.v2.phone_numbers(phone).fetch()
            is_valid = lookup.valid
        except TwilioRestException:
            # If the lookup fails, allow the message through but don't cache
            logger.debug("Twilio Lookup failed for %s — allowing send anyway", phone)
            return True

        self._redis.setex(cache_key, _PHONE_VALIDATION_CACHE_TTL, "1" if is_valid else "0")
        return is_valid

    def _check_rate_limit(self, user_id: str) -> bool:
        """
        Sliding-window rate limiter: allow at most N messages per user per hour.

        Uses a Redis sorted set where each member is a send timestamp.  On each
        call we remove entries older than 1 hour, count the remainder, and
        decide whether to allow the send.
        """
        key = f"{_RATE_LIMIT_PREFIX}{user_id}"
        now = time.time()
        window_start = now - 3600  # 1 hour ago

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, "-inf", window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, 3600)
        results = pipe.execute()

        current_count = results[1]
        if current_count >= SMS_RATE_LIMIT_PER_USER_PER_HOUR:
            return False
        return True
