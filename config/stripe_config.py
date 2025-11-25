"""
EventPulse — Stripe-specific configuration and constants.

This module centralises every Stripe-related tunable so payment logic doesn't
have magic numbers scattered across service files.  Most values were derived
from production incidents or Stripe support recommendations.

Owner: @atharvadhumal03
"""

import stripe

from config.settings import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET

# ---------------------------------------------------------------------------
# SDK initialisation
# ---------------------------------------------------------------------------

stripe.api_key = STRIPE_SECRET_KEY

# Pin the API version so Stripe dashboard upgrades don't silently break us.
# We tested against 2023-10-16; upgrading requires running the full payment
# test suite and checking webhook payload shapes.
stripe.api_version = "2023-10-16"

# Cap network retries.  The Stripe SDK defaults to 0 (no retries), but
# transient 5xx from their API is more common than you'd expect during big
# on-sale moments.  2 retries with exponential backoff is safe because
# PaymentIntent creation is already idempotent when we supply our own key.
stripe.max_network_retries = 2

# ---------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------

# Maximum clock skew (in seconds) we tolerate when verifying webhook
# signatures.  Stripe's default is 300 (5 min) but our app servers are
# behind an ALB that occasionally buffers requests, and we've seen legitimate
# webhooks arrive up to ~8 minutes after the event timestamp during traffic
# spikes.  600s (10 min) gives us headroom without meaningfully weakening
# replay protection — the idempotency layer catches actual replays anyway.
WEBHOOK_TOLERANCE_SECONDS: int = 600

WEBHOOK_SECRET: str = STRIPE_WEBHOOK_SECRET

# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

# TTL (in seconds) for idempotency keys stored in Redis.  Stripe retains
# idempotency results for 24 hours on their side, so we mirror that window
# in our own cache to avoid accidentally sending a duplicate charge if the
# client retries after our process restarts.  86400 = 24 * 60 * 60.
IDEMPOTENCY_KEY_TTL: int = 86400

# Prefix used in Redis for idempotency keys.  Namespaced to avoid collisions
# with seat-lock keys and cache entries that share the same Redis instance.
IDEMPOTENCY_KEY_PREFIX: str = "ep:stripe:idem:"

# ---------------------------------------------------------------------------
# Refund reconciliation
# ---------------------------------------------------------------------------

# How often (in seconds) the Celery beat task polls Stripe for refund status
# updates.  We can't rely solely on webhooks because refund.updated events
# are occasionally delayed or lost (Stripe status page incident SR-482,
# Nov 2023).  3600s (1 hour) is a good balance between freshness and API
# quota usage.  The task itself is lightweight — it only fetches refunds
# updated since the last run.
REFUND_CHECK_INTERVAL: int = 3600

# Maximum age (in seconds) of a pending refund before we flag it for manual
# review.  Stripe says refunds settle in 5-10 business days; 14 calendar
# days (1_209_600s) is generous.  Anything older almost certainly needs a
# support ticket.
REFUND_STALE_THRESHOLD: int = 1_209_600  # 14 days

# ---------------------------------------------------------------------------
# PaymentIntent defaults
# ---------------------------------------------------------------------------

# Stripe expects amounts in the smallest currency unit (cents for USD, paise
# for INR, etc.).  This multiplier is applied in PaymentService before sending
# to the API.  It's only correct for zero-decimal exception currencies — for
# truly zero-decimal currencies like JPY, override per-currency in the
# CURRENCY_CONFIG below.
CURRENCY_MULTIPLIER_DEFAULT: int = 100

# Currency config: maps ISO 4217 code -> { multiplier, min_charge }.
# min_charge is the smallest amount Stripe allows for that currency.
# See https://stripe.com/docs/currencies#minimum-and-maximum-charge-amounts
CURRENCY_CONFIG: dict = {
    "usd": {"multiplier": 100, "min_charge": 50},         # $0.50
    "eur": {"multiplier": 100, "min_charge": 50},         # 0.50 EUR
    "gbp": {"multiplier": 100, "min_charge": 30},         # 0.30 GBP
    "inr": {"multiplier": 100, "min_charge": 50},         # 0.50 INR
    "jpy": {"multiplier": 1,   "min_charge": 50},         # 50 JPY (zero-decimal)
    "cad": {"multiplier": 100, "min_charge": 50},         # $0.50 CAD
}

# Default currency when the event organizer hasn't specified one.
DEFAULT_CURRENCY: str = "usd"

# ---------------------------------------------------------------------------
# Platform fee
# ---------------------------------------------------------------------------

# EventPulse takes a percentage of each ticket sale as a platform fee.
# This is collected via Stripe Connect's `application_fee_amount`.
# 5.5% was negotiated with finance in Q3 2023 — it covers our Stripe
# processing cost (~2.9% + 30c) and leaves margin for ops.
PLATFORM_FEE_PERCENT: float = 5.5

# Minimum platform fee in cents.  Even for very cheap tickets ($1-2), we
# need to cover the fixed Stripe per-transaction fee.
PLATFORM_FEE_MINIMUM_CENTS: int = 50  # $0.50

# ---------------------------------------------------------------------------
# Stripe Connect (organizer payouts)
# ---------------------------------------------------------------------------

# Delay (in seconds) before funds are available in the connected account's
# Stripe balance.  We use a 7-day delay so there's time to handle disputes
# before the organizer withdraws.  Stripe default is 2 days, but that's
# too aggressive for an event platform where chargebacks can spike after
# a cancelled event.
PAYOUT_DELAY_DAYS: int = 7

# Minimum payout amount in cents.  Below this, we accumulate until the
# next threshold crossing.  Prevents micro-payouts that confuse organizers.
MINIMUM_PAYOUT_AMOUNT_CENTS: int = 1000  # $10.00

# ---------------------------------------------------------------------------
# Retry & timeout
# ---------------------------------------------------------------------------

# How long (in seconds) to wait for a single Stripe API call before we give
# up.  The default in the SDK is 80s which is way too long for a user-facing
# checkout endpoint.  15s keeps the UX snappy; if Stripe is that slow,
# something is wrong on their end and retrying won't help.
STRIPE_REQUEST_TIMEOUT: int = 15

# ---------------------------------------------------------------------------
# Promo / discount codes
# ---------------------------------------------------------------------------

# Maximum discount percentage allowed.  100% coupons exist but must be
# created by an admin, not an organizer.  Organizer-created codes are
# capped at 75%.
MAX_ORGANIZER_DISCOUNT_PERCENT: int = 75

# How many times a single promo code can be redeemed before it's
# automatically deactivated.  0 means unlimited.
DEFAULT_PROMO_REDEMPTION_LIMIT: int = 0

# ---------------------------------------------------------------------------
# Timezone handling for Stripe timestamps
# ---------------------------------------------------------------------------

# Stripe sends all webhook timestamps as UTC Unix epochs, but our events
# table stores start/end times in the organizer's local timezone (a decision
# I still regret — @atharvadhumal03).  When comparing payment.created against
# event.start_time for "door sale" vs "presale" pricing, we convert both to
# UTC first.  The helpers live in app/services/payment_service.py.
#
# NOTE: datetime.utcnow() is deprecated in 3.12+.  We use
# datetime.now(datetime.UTC) everywhere.  If you see utcnow() in old code,
# please fix it.
# Stripe API version pin
