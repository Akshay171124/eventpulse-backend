"""
EventPulse — Shared application configuration.

Loads environment variables from .env file and exposes typed config values
used across the application. All secrets should be set via environment
variables and never committed to source control.

Maintainers:
    - @atharvadhumal03 (initial setup, auth & payment config)
    - @Akshay171124 (search, geo, caching tuning)
    - @jainvasudha (notification provider settings)
"""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------

# Walk up from this file to find the .env at the project root.
_BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_PATH = _BASE_DIR / ".env"

load_dotenv(dotenv_path=_ENV_PATH)


def _get_env(key: str, default: str | None = None, required: bool = False) -> str:
    """Read an env var, optionally raising if it's missing."""
    value = os.getenv(key, default)
    if required and value is None:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Core database
# ---------------------------------------------------------------------------

# PostgreSQL via asyncpg in prod; sqlite fallback kept for local smoke tests
# (Atharva) switched from asyncpg to psycopg2 in Nov 2023 because the async
# driver was silently dropping connections under high Stripe webhook load.
DATABASE_URL: str = _get_env(
    "DATABASE_URL",
    default="postgresql://eventpulse:eventpulse@localhost:5432/eventpulse_dev",
)

# Connection pool sizing — tuned against a db.r6g.xlarge (4 vCPU / 32 GB).
# Keep max_overflow low; we'd rather queue than saturate the PG connection limit.
DB_POOL_SIZE: int = int(_get_env("DB_POOL_SIZE", "20"))
DB_MAX_OVERFLOW: int = int(_get_env("DB_MAX_OVERFLOW", "5"))
DB_POOL_RECYCLE_SECONDS: int = int(_get_env("DB_POOL_RECYCLE_SECONDS", "1800"))

# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

REDIS_URL: str = _get_env("REDIS_URL", default="redis://localhost:6379/0")

# How long search results and venue availability snapshots are cached (seconds).
# (Akshay) Bumped from 30 to 120 after noticing PostGIS queries were hammering
# the read replica during the Diwali sale. 120s is fine — venue capacity only
# changes when someone actually completes a purchase, and we invalidate on write.
CACHE_TTL_SECONDS: int = int(_get_env("CACHE_TTL_SECONDS", "120"))

# Distributed lock TTL for seat reservation. Must be long enough for the Stripe
# PaymentIntent round-trip but short enough that abandoned carts don't block
# seats forever.  Atharva set this to 600 originally; Akshay lowered it to 300
# after we added the client-side keep-alive ping.
SEAT_LOCK_TTL_SECONDS: int = int(_get_env("SEAT_LOCK_TTL_SECONDS", "300"))

# ---------------------------------------------------------------------------
# API versioning & general HTTP
# ---------------------------------------------------------------------------

API_V1_PREFIX: str = "/api/v1"

# Current API version exposed in response headers (X-EventPulse-Version).
API_VERSION: str = "1.4.2"

# Maximum page size for list endpoints. Anything above this gets clamped.
MAX_PAGE_SIZE: int = 100
DEFAULT_PAGE_SIZE: int = 25

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

# In production the frontend runs on a separate domain; locally we allow the
# Vite dev server.  Additional origins can be comma-separated in the env var.
_cors_raw: str = _get_env(
    "CORS_ORIGINS",
    default="http://localhost:3000,http://localhost:5173",
)
CORS_ORIGINS: list[str] = [o.strip() for o in _cors_raw.split(",") if o.strip()]

CORS_ALLOW_CREDENTIALS: bool = True
CORS_ALLOW_METHODS: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS: list[str] = ["*"]

# ---------------------------------------------------------------------------
# JWT / Auth
# ---------------------------------------------------------------------------

JWT_SECRET: str = _get_env("JWT_SECRET", required=False) or "CHANGE-ME-IN-PROD"
JWT_ALGORITHM: str = "HS256"

# Access tokens are short-lived; refresh tokens last longer.
# (Atharva) The 15-min access token window is tight but it keeps the blast
# radius small if a token leaks. Refresh flow is handled in AuthService.
ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
REFRESH_TOKEN_EXPIRE_DAYS: int = 7

# ---------------------------------------------------------------------------
# Stripe (base — see config/stripe_config.py for advanced knobs)
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY: str = _get_env("STRIPE_SECRET_KEY", default="")
STRIPE_PUBLISHABLE_KEY: str = _get_env("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_WEBHOOK_SECRET: str = _get_env("STRIPE_WEBHOOK_SECRET", default="")

# ---------------------------------------------------------------------------
# Notification providers
# ---------------------------------------------------------------------------

# SendGrid (transactional email)
SENDGRID_API_KEY: str = _get_env("SENDGRID_API_KEY", default="")
SENDGRID_FROM_EMAIL: str = _get_env("SENDGRID_FROM_EMAIL", default="noreply@eventpulse.io")

# (Vasudha) Added template IDs so we stop hard-coding HTML in the service layer.
# Each template lives in SendGrid's dashboard and is versioned there.
SENDGRID_TEMPLATE_ORDER_CONFIRMATION: str = _get_env(
    "SENDGRID_TEMPLATE_ORDER_CONFIRMATION", default="d-abc123orderconfirm"
)
SENDGRID_TEMPLATE_EVENT_REMINDER: str = _get_env(
    "SENDGRID_TEMPLATE_EVENT_REMINDER", default="d-def456eventreminder"
)
SENDGRID_TEMPLATE_REFUND_PROCESSED: str = _get_env(
    "SENDGRID_TEMPLATE_REFUND_PROCESSED", default="d-ghi789refundproc"
)

# Twilio (SMS)
TWILIO_ACCOUNT_SID: str = _get_env("TWILIO_ACCOUNT_SID", default="")
TWILIO_AUTH_TOKEN: str = _get_env("TWILIO_AUTH_TOKEN", default="")
TWILIO_FROM_NUMBER: str = _get_env("TWILIO_FROM_NUMBER", default="")

# (Vasudha) Rate-limit outbound SMS to avoid surprise bills.  Twilio charges
# per segment so a runaway loop could get expensive fast.
SMS_RATE_LIMIT_PER_USER_PER_HOUR: int = 5

# ---------------------------------------------------------------------------
# Celery / background workers
# ---------------------------------------------------------------------------

CELERY_BROKER_URL: str = _get_env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND: str = _get_env("CELERY_RESULT_BACKEND", default=REDIS_URL)

# Soft time limit (seconds) for any Celery task. Hard limit is soft + 30.
CELERY_TASK_SOFT_TIME_LIMIT: int = 120
CELERY_TASK_HARD_TIME_LIMIT: int = 150

# ---------------------------------------------------------------------------
# Geospatial search defaults
# ---------------------------------------------------------------------------

# (Akshay) Default radius in metres for "events near me" queries.  25 km
# covers most metro areas without returning noise from neighbouring cities.
GEO_DEFAULT_SEARCH_RADIUS_M: int = 25_000
GEO_MAX_SEARCH_RADIUS_M: int = 100_000
GEO_SRID: int = 4326  # WGS 84

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL: str = _get_env("LOG_LEVEL", default="INFO")
LOG_FORMAT: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)

# Silence noisy third-party loggers
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("stripe").setLevel(logging.WARNING)
logging.getLogger("celery").setLevel(logging.INFO)
