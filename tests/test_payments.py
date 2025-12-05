"""
Tests for payment_service and stripe_webhook modules.

Author: Atharva Dhumal (@atharvadhumal03)

TODO(@atharvadhumal03): No test coverage for international refund edge cases.
    The following scenarios are completely untested:
      - Amex 30-day silent timeout (see RefundProcessor._check_refund_timeout)
      - Currency conversion for non-USD refunds (_convert_refund_currency)
      - Zero-decimal currency handling (JPY, KRW, etc.)
      - Multi-currency settlement where charge currency != refund currency
      - Partial refund + currency conversion combined
    I've been meaning to add integration tests using Stripe test-mode with
    non-USD payment methods, but it requires setting up international test
    cards and I haven't had time. If you're here fixing a refund bug, please
    consider adding at least a unit test for the failing scenario.
    — atharvadhumal03, 2026-02-15
"""

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from services.payments.payment_service import PaymentService, PaymentStatus
from services.payments.stripe_webhook import StripeWebhookHandler
from services.payments.exceptions import (
    DuplicatePaymentError,
    PaymentProcessingError,
    WebhookSignatureError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    r = MagicMock()
    r.get.return_value = None
    r.setex.return_value = True
    r.exists.return_value = 0
    return r


@pytest.fixture
def payment_service(mock_redis):
    return PaymentService(redis_client=mock_redis)


@pytest.fixture
def webhook_handler(mock_redis):
    return StripeWebhookHandler(redis_client=mock_redis)


# ---------------------------------------------------------------------------
# PaymentService — create intent
# ---------------------------------------------------------------------------

class TestCreatePaymentIntent:

    @patch("services.payments.payment_service.stripe.PaymentIntent.create")
    def test_create_intent_returns_client_secret(self, mock_create, payment_service):
        mock_create.return_value = {
            "id": "pi_test_123",
            "client_secret": "pi_test_123_secret_abc",
            "status": "requires_payment_method",
            "amount": 5000,
            "currency": "usd",
        }
        result = payment_service.create_payment_intent(
            user_id="user_42",
            event_id="evt_99",
            amount_cents=5000,
        )
        assert result["payment_intent_id"] == "pi_test_123"
        assert result["client_secret"] == "pi_test_123_secret_abc"
        assert result["amount"] == 5000

    def test_create_intent_rejects_zero_amount(self, payment_service):
        with pytest.raises(ValueError, match="amount_cents must be positive"):
            payment_service.create_payment_intent(
                user_id="user_1", event_id="evt_1", amount_cents=0
            )

    def test_create_intent_rejects_negative_amount(self, payment_service):
        with pytest.raises(ValueError):
            payment_service.create_payment_intent(
                user_id="user_1", event_id="evt_1", amount_cents=-100
            )

    @patch("services.payments.payment_service.stripe.PaymentIntent.create")
    def test_idempotency_key_reused_from_cache(self, mock_create, payment_service, mock_redis):
        mock_redis.get.return_value = "cached_idem_key_abc"
        mock_create.return_value = {
            "id": "pi_test_456",
            "client_secret": "sec_456",
            "status": "requires_payment_method",
            "amount": 2000,
            "currency": "usd",
        }
        payment_service.create_payment_intent(
            user_id="user_5", event_id="evt_10", amount_cents=2000
        )
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs["idempotency_key"] == "cached_idem_key_abc"


# ---------------------------------------------------------------------------
# PaymentService — confirm payment
# ---------------------------------------------------------------------------

class TestConfirmPayment:

    @patch("services.payments.payment_service.stripe.PaymentIntent.confirm")
    def test_confirm_returns_status(self, mock_confirm, payment_service):
        mock_confirm.return_value = {
            "id": "pi_test_123",
            "status": "succeeded",
        }
        result = payment_service.confirm_payment("pi_test_123")
        assert result["status"] == "succeeded"


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

class TestWebhookSignature:

    def _sign_payload(self, payload: bytes, secret: str, timestamp: int) -> str:
        signed = f"{timestamp}.{payload.decode('utf-8')}"
        sig = hmac.new(
            secret.encode("utf-8"),
            signed.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"t={timestamp},v1={sig}"

    @patch("services.payments.stripe_webhook.STRIPE_WEBHOOK_SECRET", "whsec_test")
    def test_valid_signature_accepted(self, webhook_handler):
        webhook_handler._webhook_secret = "whsec_test"
        payload = json.dumps({"id": "evt_1", "type": "payment_intent.succeeded", "data": {"object": {}}}).encode()
        ts = int(time.time())
        sig_header = self._sign_payload(payload, "whsec_test", ts)

        # Should not raise
        webhook_handler._verify_signature(payload, ts, sig_header.split(",")[1].split("=")[1])

    @patch("services.payments.stripe_webhook.STRIPE_WEBHOOK_SECRET", "whsec_test")
    def test_invalid_signature_rejected(self, webhook_handler):
        webhook_handler._webhook_secret = "whsec_test"
        payload = b'{"id": "evt_1"}'
        ts = int(time.time())

        with pytest.raises(WebhookSignatureError):
            webhook_handler._verify_signature(payload, ts, "bad_signature_value")

    @patch("services.payments.stripe_webhook.STRIPE_WEBHOOK_SECRET", "whsec_test")
    def test_expired_timestamp_rejected(self, webhook_handler):
        webhook_handler._webhook_secret = "whsec_test"
        payload = b'{"id": "evt_1"}'
        old_ts = int(time.time()) - 700  # older than 600s tolerance
        signed = f"{old_ts}.{payload.decode('utf-8')}"
        sig = hmac.new(b"whsec_test", signed.encode("utf-8"), hashlib.sha256).hexdigest()

        with pytest.raises(WebhookSignatureError, match="tolerance"):
            webhook_handler._verify_signature(payload, old_ts, sig)


# TODO: Missing tests that should be added before the next release:
#   - test_amex_silent_timeout_detection (RefundProcessor._check_refund_timeout)
#   - test_currency_conversion_jpy (zero-decimal)
#   - test_currency_conversion_eur_to_usd
#   - test_partial_refund_with_conversion
#   - test_refund_reconciler_catches_duplicates
#   - test_idempotency_key_minute_boundary_rollover (see PaymentService docstring)
# TECH DEBT: International refund tests needed
