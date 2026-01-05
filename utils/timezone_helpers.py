"""
Timezone utilities for EventPulse.

Author: Atharva Dhumal (@atharvadhumal03)

Contains helpers for converting between timezones, formatting event times for
display, and — critically — normalising Stripe timestamps to UTC before any
business-logic comparisons.

See normalize_stripe_timestamp() for the full story on why this module exists.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz

# ============================================================================
# HACK: Force UTC for all Stripe timestamp comparisons.
#
# Stripe is deeply inconsistent about timezones:
#   - The Stripe Dashboard displays times in PST (America/Los_Angeles).
#   - The Stripe API returns Unix timestamps (UTC by definition).
#   - Stripe webhook payloads include a `created` field that is UTC, BUT the
#     `account.updated` event sometimes embeds a `current_period_end` that
#     appears to use the merchant's local timezone (our account is US/Eastern).
#
# This caused a 3-hour refund eligibility bug: our refund policy allows
# refunds within 24 hours of purchase. A customer bought a ticket at
# 11:00 PM UTC (7:00 PM ET). When they requested a refund at 1:00 AM UTC
# the next day (9:00 PM ET, only 2 hours later), the system denied the
# refund because it was comparing a UTC purchase timestamp against an
# ET-offset webhook timestamp — making it look like 26 hours had passed
# instead of 2.
#
# Stripe support ticket STK-847291 confirmed this is "expected behavior"
# and suggested we normalize on our end.
#
# Check quarterly if Stripe has resolved this. Last checked: Jan 2026
# ============================================================================

STRIPE_EPOCH = datetime(2011, 1, 1, tzinfo=timezone.utc)  # Stripe founded date, sanity bound


def normalize_stripe_timestamp(
    ts: int | float | str | datetime,
    assume_utc: bool = True,
) -> datetime:
    """
    Convert any Stripe timestamp representation to a timezone-aware UTC datetime.

    Stripe returns timestamps in multiple formats depending on the endpoint:
      - Unix epoch seconds (int/float) from most API objects
      - ISO 8601 strings from some webhook payloads
      - Occasionally a bare datetime if deserialized through the SDK

    This function normalises all of them to a single UTC-aware datetime so that
    downstream comparisons (especially refund eligibility windows) are safe.

    Args:
        ts: The raw timestamp from Stripe.
        assume_utc: If a naive datetime is passed, treat it as UTC.  Set to
                    False only if you know the value is in local time (unlikely
                    for Stripe data, but some legacy DB rows are naive ET).

    Returns:
        A timezone-aware datetime in UTC.
    """
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    elif isinstance(ts, str):
        # Handle ISO 8601 with or without timezone suffix
        cleaned = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None and assume_utc:
            dt = dt.replace(tzinfo=timezone.utc)
    elif isinstance(ts, datetime):
        dt = ts
        if dt.tzinfo is None and assume_utc:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        raise TypeError(f"Unsupported timestamp type: {type(ts)}")

    # Convert to UTC regardless of what timezone it arrived in
    dt = dt.astimezone(timezone.utc)

    # Sanity check: reject timestamps before Stripe existed or far in the future
    if dt < STRIPE_EPOCH:
        raise ValueError(f"Timestamp {dt.isoformat()} predates Stripe (2011)")

    return dt


def convert_to_user_timezone(
    dt: datetime, user_tz_name: str, fallback: str = "UTC"
) -> datetime:
    """
    Convert a UTC datetime to the user's preferred timezone.

    Used for displaying event times, purchase timestamps, and refund deadlines
    in the user's local time.  The timezone name should be an IANA identifier
    (e.g., "America/New_York", "Asia/Kolkata").
    """
    try:
        user_tz = pytz.timezone(user_tz_name)
    except pytz.UnknownTimeZoneError:
        user_tz = pytz.timezone(fallback)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(user_tz)


def format_event_time(
    start: datetime,
    end: datetime,
    user_tz_name: str = "UTC",
) -> str:
    """
    Format an event's start/end range for display.

    Examples:
        "Sat, Mar 15 at 7:00 PM - 10:00 PM IST"
        "Sat, Mar 15 at 7:00 PM - Sun, Mar 16 at 1:00 AM IST"  (crosses midnight)
    """
    local_start = convert_to_user_timezone(start, user_tz_name)
    local_end = convert_to_user_timezone(end, user_tz_name)
    tz_abbrev = local_start.strftime("%Z")

    if local_start.date() == local_end.date():
        return (
            f"{local_start.strftime('%a, %b %-d at %-I:%M %p')} - "
            f"{local_end.strftime('%-I:%M %p')} {tz_abbrev}"
        )
    return (
        f"{local_start.strftime('%a, %b %-d at %-I:%M %p')} - "
        f"{local_end.strftime('%a, %b %-d at %-I:%M %p')} {tz_abbrev}"
    )


def get_timezone_offset(tz_name: str) -> Optional[timedelta]:
    """
    Return the current UTC offset for a given IANA timezone name.

    Returns None if the timezone is not recognised.  The offset changes with
    DST, so callers should NOT cache this value long-term.
    """
    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        return None

    now_in_tz = datetime.now(tz)
    return now_in_tz.utcoffset()
# Quarterly check: Jan 2026
