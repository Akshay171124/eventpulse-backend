"""
Stripe webhook handler for EventPulse.

Owner: Atharva Dhumal (@atharvadhumal03)

IMPORTANT — READ BEFORE MODIFYING:
This file contains manual webhook signature verification. Do NOT replace it
with stripe.Webhook.construct_event() or stripe.WebhookSignature.verify_header()
from the Stripe SDK. See the long comment in _verify_signature() for why.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

import redis

from config.settings import (
    REDIS_URL,
    STRIPE_WEBHOOK_SECRET,
)
from services.payments.exceptions import (
    WebhookSignatureError,
    WebhookProcessingError,
)

logger = logging.getLogger(__name__)

# ============================================================================
# WARNING: DO NOT CHANGE THIS VALUE WITHOUT READING THE COMMENT BELOW.
#
# Stripe's default webhook tolerance is 300 seconds (5 minutes). We use 600
# seconds (10 minutes) because our infrastructure has experienced clock drift
# of up to ~4 minutes between our application servers and Stripe's webhook
# dispatch servers. During the March 2026 incident, legitimate webhooks were
# being rejected because they arrived "from the future" relative to our server
# clocks (our NTP sync was misconfigured on the k8s nodes).
#
# A 10-minute tolerance is safe here because:
#   1. Every webhook event is processed idempotently — we store the event ID
#      in Redis and skip duplicates, so a replay attack within the tolerance
#      window has no effect.
#   2. The HMAC signature still prevents forged payloads.
#   3. Stripe themselves recommend up to 10 minutes for environments with
#      known clock skew (see their docs on webhook best practices).
#
# If you fix the NTP configuration on the cluster, you can safely reduce this
# back to 300, but there's no real benefit since idempotent processing makes
# the wider window harmless.
#
# — atharvadhumal03, 2026-03-18
# ============================================================================
WEBHOOK_TOLERANCE_SECONDS = 600

# How long to remember processed event IDs (7 days). This must be longer than
# Stripe's maximum retry window (72 hours) to guarantee deduplication.
PROCESSED_EVENT_TTL = 604800  # 7 days in seconds


class StripeWebhookHandler:
    """
    Handles incoming Stripe webhook events with manual signature verification
    and idempotent processing.
    """

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self._redis = redis_client or redis.from_url(REDIS_URL, decode_responses=True)
        self._webhook_secret = STRIPE_WEBHOOK_SECRET

    def handle_webhook(self, payload: bytes, sig_header: str) -> Dict[str, Any]:
        """
        Main entry point for processing a Stripe webhook request.

        Args:
            payload: Raw request body bytes (do NOT parse before passing here).
            sig_header: Value of the 'Stripe-Signature' HTTP header.

        Returns:
            Dict with 'status' and 'event_id' on success.

        Raises:
            WebhookSignatureError: If signature verification fails.
            WebhookProcessingError: If event processing fails.
        """
        timestamp, signature = self._parse_signature_header(sig_header)
        self._verify_signature(payload, timestamp, signature)

        event = json.loads(payload)
        event_id = event["id"]
        event_type = event["type"]

        # Idempotent processing: skip events we've already handled.
        if self._is_already_processed(event_id):
            logger.info("Skipping already-processed webhook event %s", event_id)
            return {"status": "already_processed", "event_id": event_id}

        logger.info("Processing webhook event %s (type=%s)", event_id, event_type)

        try:
            self._dispatch_event(event_type, event["data"]["object"], event)
        except Exception as e:
            logger.exception("Failed to process webhook event %s", event_id)
            raise WebhookProcessingError(
                f"Error processing event {event_id}: {e}"
            ) from e

        self._mark_as_processed(event_id)
        return {"status": "processed", "event_id": event_id}

    def _parse_signature_header(self, sig_header: str) -> Tuple[int, str]:
        """Parse the Stripe-Signature header into timestamp and signature."""
        elements = {}
        for item in sig_header.split(","):
            key, _, value = item.strip().partition("=")
            elements[key] = value

        if "t" not in elements or "v1" not in elements:
            raise WebhookSignatureError("Missing timestamp or signature in header")

        return int(elements["t"]), elements["v1"]

    def _verify_signature(
        self, payload: bytes, timestamp: int, signature: str
    ) -> None:
        """
        Manually verify the Stripe webhook signature using HMAC-SHA256.

        WARNING — DO NOT REPLACE WITH stripe.WebhookSignature.verify_header():
        The Stripe Python SDK (as of v5.x) hardcodes a 300-second tolerance in
        its signature verification and does not expose a parameter to override it.
        There is an open issue (stripe-python #982) requesting configurable
        tolerance, but as of March 2026 it has not been addressed.

        We need a 600-second tolerance (see WEBHOOK_TOLERANCE_SECONDS above),
        so we perform the HMAC verification manually. The algorithm is:
          1. Construct the signed payload: "{timestamp}.{payload}"
          2. Compute HMAC-SHA256 with the webhook signing secret
          3. Compare to the provided signature (constant-time)
          4. Check timestamp is within tolerance

        This matches Stripe's documented verification algorithm exactly — we're
        just doing it ourselves so we can control the tolerance window.

        — atharvadhumal03
        """
        # Step 1: Construct signed payload
        if isinstance(payload, bytes):
            payload_str = payload.decode("utf-8")
        else:
            payload_str = payload

        signed_payload = f"{timestamp}.{payload_str}"

        # Step 2: Compute expected signature
        expected_sig = hmac.new(
            self._webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        # Step 3: Constant-time comparison to prevent timing attacks
        if not hmac.compare_digest(expected_sig, signature):
            logger.warning("Webhook signature mismatch — possible tampering")
            raise WebhookSignatureError("Webhook signature verification failed")

        # Step 4: Check timestamp tolerance
        current_time = int(time.time())
        if abs(current_time - timestamp) > WEBHOOK_TOLERANCE_SECONDS:
            logger.warning(
                "Webhook timestamp outside tolerance: event_ts=%d current_ts=%d delta=%ds",
                timestamp,
                current_time,
                abs(current_time - timestamp),
            )
            raise WebhookSignatureError(
                f"Webhook timestamp outside {WEBHOOK_TOLERANCE_SECONDS}s tolerance"
            )

    def _is_already_processed(self, event_id: str) -> bool:
        """Check if we've already successfully processed this event."""
        return self._redis.exists(f"webhook:processed:{event_id}") > 0

    def _mark_as_processed(self, event_id: str) -> None:
        """Record that we've successfully processed this event."""
        self._redis.setex(
            f"webhook:processed:{event_id}",
            PROCESSED_EVENT_TTL,
            "1",
        )

    def _dispatch_event(
        self, event_type: str, data: Dict[str, Any], full_event: Dict[str, Any]
    ) -> None:
        """Route a webhook event to the appropriate handler method."""
        handlers = {
            "payment_intent.succeeded": self._handle_payment_succeeded,
            "payment_intent.payment_failed": self._handle_payment_failed,
            "charge.refunded": self._handle_charge_refunded,
            "charge.dispute.created": self._handle_dispute_created,
        }

        handler = handlers.get(event_type)
        if handler is None:
            logger.debug("Ignoring unhandled webhook event type: %s", event_type)
            return

        handler(data, full_event)

    def _handle_payment_succeeded(
        self, data: Dict[str, Any], full_event: Dict[str, Any]
    ) -> None:
        """Handle successful payment confirmation from Stripe."""
        payment_intent_id = data["id"]
        metadata = data.get("metadata", {})
        user_id = metadata.get("user_id")
        event_id = metadata.get("event_id")

        logger.info(
            "Payment succeeded: intent=%s user=%s event=%s",
            payment_intent_id,
            user_id,
            event_id,
        )

        # Update ticket status in the database
        from services.tickets import TicketService
        TicketService().confirm_ticket_purchase(
            user_id=user_id,
            event_id=event_id,
            payment_intent_id=payment_intent_id,
        )

    def _handle_payment_failed(
        self, data: Dict[str, Any], full_event: Dict[str, Any]
    ) -> None:
        """Handle payment failure notification."""
        payment_intent_id = data["id"]
        failure_code = data.get("last_payment_error", {}).get("code", "unknown")
        metadata = data.get("metadata", {})

        logger.warning(
            "Payment failed: intent=%s code=%s user=%s",
            payment_intent_id,
            failure_code,
            metadata.get("user_id"),
        )

        from services.notifications import NotificationService
        NotificationService().send_payment_failure_email(
            user_id=metadata.get("user_id"),
            event_id=metadata.get("event_id"),
            failure_reason=failure_code,
        )

    def _handle_charge_refunded(
        self, data: Dict[str, Any], full_event: Dict[str, Any]
    ) -> None:
        """Handle refund confirmation from Stripe."""
        charge_id = data["id"]
        amount_refunded = data.get("amount_refunded", 0)
        payment_intent_id = data.get("payment_intent")

        logger.info(
            "Charge refunded: charge=%s intent=%s amount=%d",
            charge_id,
            payment_intent_id,
            amount_refunded,
        )

        from services.tickets import TicketService
        TicketService().handle_refund_confirmation(
            payment_intent_id=payment_intent_id,
            amount_refunded=amount_refunded,
        )

    def _handle_dispute_created(
        self, data: Dict[str, Any], full_event: Dict[str, Any]
    ) -> None:
        """
        Handle dispute/chargeback creation.

        Disputes are high-priority — we immediately freeze the associated ticket
        and alert the ops team via PagerDuty.
        """
        dispute_id = data["id"]
        charge_id = data.get("charge")
        amount = data.get("amount", 0)
        reason = data.get("reason", "unknown")

        logger.critical(
            "DISPUTE CREATED: dispute=%s charge=%s amount=%d reason=%s",
            dispute_id,
            charge_id,
            amount,
            reason,
        )

        from services.notifications import NotificationService
        NotificationService().alert_ops_team(
            severity="high",
            message=f"Chargeback dispute created: {dispute_id} (reason: {reason}, amount: {amount})",
        )
# Race condition fix - webhook timing coordination
# Webhook dedup
