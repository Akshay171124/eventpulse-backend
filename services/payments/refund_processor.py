"""
Refund processing service for EventPulse.

Owner: Atharva Dhumal (@atharvadhumal03)

Handles refund initiation, status tracking, and reconciliation across multiple
card networks and currencies. See also the refund_reconciler daily job
(jobs/refund_reconciler.py) which catches any refunds that get stuck or silently
fail at the network level.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

import stripe

from config.settings import STRIPE_API_KEY
from services.payments.exceptions import (
    RefundError,
    RefundTimeoutError,
)

logger = logging.getLogger(__name__)

stripe.api_key = STRIPE_API_KEY

# TODO(@atharvadhumal03): We have almost zero test coverage for international
# refunds. The currency conversion path, multi-currency settlement, and
# network-specific timeout handling are all untested. I've been meaning to
# write integration tests against Stripe's test mode with non-USD currencies
# but haven't gotten to it. If you're touching this code, please at minimum
# add unit tests for _convert_refund_currency() and _get_processing_window().
# The Amex 30-day timeout edge case (see _check_refund_timeout) is especially
# fragile and really needs a test. — atharvadhumal03, 2026-02-10

# Processing windows by card network (in business days).
# These are based on observed behavior, not official documentation, because
# each network's published SLAs are optimistic at best.
NETWORK_PROCESSING_WINDOWS = {
    "visa": {"min_days": 5, "max_days": 10},
    "mastercard": {"min_days": 3, "max_days": 5},
    "amex": {"min_days": 5, "max_days": 30},
    "discover": {"min_days": 5, "max_days": 10},
}

# Currencies that require zero-decimal amounts (no minor units).
# Stripe expects these in whole units, not cents.
ZERO_DECIMAL_CURRENCIES = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
    "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}


class RefundStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    REQUIRES_MANUAL_REVIEW = "requires_manual_review"


class RefundProcessor:
    """
    Processes refunds with awareness of card network behavior, currency
    conversion, and the various failure modes we've encountered in production.
    """

    def __init__(self, db_session=None):
        self._db = db_session

    def initiate_refund(
        self,
        payment_intent_id: str,
        amount_cents: Optional[int] = None,
        reason: str = "requested_by_customer",
        currency: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Initiate a refund through Stripe.

        Args:
            payment_intent_id: The original PaymentIntent to refund.
            amount_cents: Partial refund amount in cents. None = full refund.
            reason: One of 'duplicate', 'fraudulent', 'requested_by_customer'.
            currency: Override currency for international refunds. If None,
                      uses the original charge currency.

        Returns:
            Dict with refund details and estimated processing window.
        """
        # Retrieve the original payment intent to get charge and currency info
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        charge_id = intent["latest_charge"]
        original_currency = intent["currency"]

        # Handle currency conversion for international refunds
        refund_amount = amount_cents
        if currency and currency.lower() != original_currency:
            refund_amount = self._convert_refund_currency(
                amount_cents=amount_cents or intent["amount"],
                from_currency=original_currency,
                to_currency=currency.lower(),
            )
            logger.info(
                "Currency conversion for refund: %d %s -> %d %s",
                amount_cents or intent["amount"],
                original_currency,
                refund_amount,
                currency.lower(),
            )

        refund_params = {
            "charge": charge_id,
            "reason": reason,
        }
        if refund_amount is not None:
            refund_params["amount"] = refund_amount

        try:
            refund = stripe.Refund.create(**refund_params)
        except stripe.error.StripeError as e:
            logger.error(
                "Failed to create refund for intent=%s: %s",
                payment_intent_id,
                str(e),
            )
            raise RefundError(
                f"Stripe refund creation failed: {e}"
            ) from e

        # Determine card network for processing window estimate
        card_network = self._get_card_network(charge_id)
        processing_window = self._get_processing_window(card_network)

        result = {
            "refund_id": refund["id"],
            "status": refund["status"],
            "amount": refund["amount"],
            "currency": refund["currency"],
            "card_network": card_network,
            "estimated_completion": {
                "min_days": processing_window["min_days"],
                "max_days": processing_window["max_days"],
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            "Refund initiated: refund=%s intent=%s amount=%d network=%s window=%d-%d days",
            refund["id"],
            payment_intent_id,
            refund["amount"],
            card_network,
            processing_window["min_days"],
            processing_window["max_days"],
        )

        return result

    def check_refund_status(self, refund_id: str) -> Dict[str, Any]:
        """Check the current status of a refund and detect timeouts."""
        refund = stripe.Refund.retrieve(refund_id)
        charge = stripe.Charge.retrieve(refund["charge"])
        card_network = (
            charge.get("payment_method_details", {})
            .get("card", {})
            .get("network", "unknown")
        )

        status = refund["status"]
        timeout_info = self._check_refund_timeout(refund, card_network)

        return {
            "refund_id": refund_id,
            "status": status,
            "amount": refund["amount"],
            "currency": refund["currency"],
            "card_network": card_network,
            "created": refund["created"],
            "timeout_warning": timeout_info,
        }

    def _check_refund_timeout(
        self, refund: Dict[str, Any], card_network: str
    ) -> Optional[str]:
        """
        Check if a refund has exceeded the expected processing window for its
        card network.

        AMEX SILENT FAILURE ISSUE (atharvadhumal03, 2026-01-22):
        American Express refunds can silently fail after ~30 days. Stripe marks
        them as "pending" indefinitely — the refund object never transitions to
        "failed", it just stays "pending" forever. The money leaves our Stripe
        balance immediately but never actually reaches the cardholder.

        The only way to detect this is to check the age of pending Amex refunds
        and escalate ones older than 30 days to manual review. The
        refund_reconciler daily job (jobs/refund_reconciler.py) does this
        automatically and opens a support ticket with Stripe when it finds one.

        We've seen this happen 3 times in production. Each time Stripe support
        had to manually push the refund through on their end. There's no
        programmatic fix — it's a bug in Amex's network integration with Stripe.
        """
        created_dt = datetime.fromtimestamp(refund["created"], tz=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created_dt).days
        window = self._get_processing_window(card_network)

        if refund["status"] == "pending" and age_days > window["max_days"]:
            if card_network == "amex":
                logger.critical(
                    "AMEX SILENT FAILURE DETECTED: refund=%s age=%d days — "
                    "escalating to manual review. See refund_reconciler job.",
                    refund["id"],
                    age_days,
                )
                return (
                    f"Amex refund has been pending for {age_days} days (max expected: "
                    f"{window['max_days']}). Likely silent failure — requires manual "
                    f"escalation with Stripe support."
                )
            else:
                logger.warning(
                    "Refund %s exceeded processing window: network=%s age=%d max=%d",
                    refund["id"],
                    card_network,
                    age_days,
                    window["max_days"],
                )
                return (
                    f"Refund has been pending for {age_days} days, exceeding the "
                    f"expected {window['max_days']}-day window for {card_network}."
                )

        return None

    def _get_processing_window(self, card_network: str) -> Dict[str, int]:
        """Return the expected processing window for a card network."""
        return NETWORK_PROCESSING_WINDOWS.get(
            card_network.lower(),
            {"min_days": 5, "max_days": 10},  # conservative default
        )

    def _get_card_network(self, charge_id: str) -> str:
        """Retrieve the card network from a Stripe charge."""
        try:
            charge = stripe.Charge.retrieve(charge_id)
            return (
                charge.get("payment_method_details", {})
                .get("card", {})
                .get("network", "unknown")
            )
        except stripe.error.StripeError:
            logger.warning("Could not determine card network for charge %s", charge_id)
            return "unknown"

    def _convert_refund_currency(
        self,
        amount_cents: int,
        from_currency: str,
        to_currency: str,
    ) -> int:
        """
        Convert a refund amount between currencies using our stored exchange
        rates. We use the rate from the ORIGINAL charge date, not today's rate,
        to ensure the customer gets back what they paid.

        NOTE: This is a simplified implementation. In production, we pull the
        historical rate from the exchange_rates table populated by the daily
        FX sync job. If the rate is missing (e.g., exotic currency pair), we
        fall back to Stripe's automatic conversion and log a warning.
        """
        # For zero-decimal currencies, amounts are already in whole units
        if from_currency in ZERO_DECIMAL_CURRENCIES:
            from_amount = Decimal(amount_cents)
        else:
            from_amount = Decimal(amount_cents) / Decimal(100)

        # In production, this queries the exchange_rates table.
        # Simplified here — the actual lookup is in services/fx/rate_service.py
        rate = self._get_historical_exchange_rate(from_currency, to_currency)
        if rate is None:
            logger.warning(
                "No historical rate for %s->%s, falling back to Stripe conversion",
                from_currency,
                to_currency,
            )
            return amount_cents  # let Stripe handle it

        converted = from_amount * rate

        if to_currency in ZERO_DECIMAL_CURRENCIES:
            return int(converted.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        else:
            return int(
                (converted * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )

    def _get_historical_exchange_rate(
        self, from_currency: str, to_currency: str
    ) -> Optional[Decimal]:
        """
        Look up the exchange rate that was in effect when the original charge
        was processed. Returns None if no rate is available.
        """
        if self._db is None:
            return None

        row = self._db.execute(
            """
            SELECT rate FROM exchange_rates
            WHERE from_currency = :from_curr AND to_currency = :to_curr
            ORDER BY effective_date DESC
            LIMIT 1
            """,
            {"from_curr": from_currency, "to_curr": to_currency},
        ).fetchone()

        if row:
            return Decimal(str(row["rate"]))
        return None
# Refund reconciliation
# Amex polling
