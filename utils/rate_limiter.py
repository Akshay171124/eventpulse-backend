"""
Redis-backed sliding-window rate limiter for API endpoints.

Author: Akshay Prajapati (@Akshay171124)

Supports both IP-based and authenticated-user-based limiting.  Each endpoint
can declare its own (requests, window) tuple via ENDPOINT_LIMITS.

Implementation uses a Redis sorted set per (identifier, endpoint) pair.
Members are microsecond timestamps; on each request we trim expired entries,
count the survivors, and decide whether to allow or reject.

Why sorted sets instead of a simple INCR + EXPIRE?
  - INCR/EXPIRE creates a fixed window that resets abruptly.  A user can
    send 100 requests at 11:59:59 and another 100 at 12:00:00.
  - The sorted-set approach gives us a true sliding window with no burst
    at the boundary.  Slightly more Redis overhead, but negligible at our
    scale (~2 K RPM peak).
"""

import logging
import time
from typing import Optional, Tuple

import redis

from config.settings import REDIS_URL

logger = logging.getLogger(__name__)

# (max_requests, window_seconds) per endpoint pattern.
# The key is a prefix that is matched against the request path.
ENDPOINT_LIMITS: dict[str, Tuple[int, int]] = {
    "/api/v1/payments": (30, 60),       # 30 req / min — payment endpoints are expensive
    "/api/v1/auth/login": (10, 60),     # 10 req / min — brute-force protection
    "/api/v1/auth/register": (5, 300),  # 5 req / 5 min — registration abuse
    "/api/v1/events": (60, 60),         # 60 req / min — read-heavy, more generous
    "/api/v1/search": (40, 60),         # 40 req / min — PostGIS queries aren't free
    "default": (100, 60),               # fallback for unlisted endpoints
}

_KEY_PREFIX = "rl:"


class RateLimiter:
    """
    Sliding-window rate limiter backed by Redis sorted sets.

    Usage (FastAPI middleware example)::

        limiter = RateLimiter()

        @app.middleware("http")
        async def rate_limit_middleware(request, call_next):
            identifier = request.client.host
            if request.state.user_id:
                identifier = request.state.user_id
            allowed, info = limiter.check(identifier, request.url.path)
            if not allowed:
                return JSONResponse(status_code=429, content=info)
            return await call_next(request)
    """

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self._redis = redis_client or redis.from_url(REDIS_URL, decode_responses=True)

    def check(
        self, identifier: str, endpoint: str
    ) -> Tuple[bool, dict]:
        """
        Check whether a request from *identifier* to *endpoint* is allowed.

        Args:
            identifier: IP address or authenticated user ID.
            endpoint: The request path (e.g., "/api/v1/payments/checkout").

        Returns:
            (allowed, info) where *info* contains rate-limit metadata suitable
            for X-RateLimit-* response headers or a 429 body.
        """
        max_requests, window_seconds = self._resolve_limit(endpoint)
        key = self._make_key(identifier, endpoint)
        now = time.time()
        window_start = now - window_seconds

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, "-inf", window_start)
        pipe.zcard(key)
        pipe.zadd(key, {f"{now}": now})
        pipe.expire(key, window_seconds)
        results = pipe.execute()

        current_count = results[1]  # count BEFORE adding the new request
        remaining = max(0, max_requests - current_count - 1)
        allowed = current_count < max_requests

        info = {
            "limit": max_requests,
            "remaining": remaining,
            "window_seconds": window_seconds,
            "reset_at": int(now + window_seconds),
        }

        if not allowed:
            info["error"] = "Rate limit exceeded. Please try again later."
            logger.info(
                "Rate limit hit: identifier=%s endpoint=%s count=%d limit=%d",
                identifier, endpoint, current_count, max_requests,
            )

        return allowed, info

    def check_ip(self, ip_address: str, endpoint: str) -> Tuple[bool, dict]:
        """Convenience wrapper for IP-based rate limiting."""
        return self.check(f"ip:{ip_address}", endpoint)

    def check_user(self, user_id: str, endpoint: str) -> Tuple[bool, dict]:
        """Convenience wrapper for user-based rate limiting."""
        return self.check(f"user:{user_id}", endpoint)

    def reset(self, identifier: str, endpoint: str) -> None:
        """Clear the rate-limit window for a given identifier + endpoint."""
        key = self._make_key(identifier, endpoint)
        self._redis.delete(key)

    def _resolve_limit(self, endpoint: str) -> Tuple[int, int]:
        """Match the endpoint against ENDPOINT_LIMITS using prefix matching."""
        for prefix, limits in ENDPOINT_LIMITS.items():
            if prefix != "default" and endpoint.startswith(prefix):
                return limits
        return ENDPOINT_LIMITS["default"]

    def _make_key(self, identifier: str, endpoint: str) -> str:
        """Build the Redis key for a given identifier and endpoint prefix."""
        # Normalise the endpoint to its limit prefix so that
        # /api/v1/payments/checkout and /api/v1/payments/status share a bucket.
        prefix = self._resolve_endpoint_prefix(endpoint)
        return f"{_KEY_PREFIX}{identifier}:{prefix}"

    @staticmethod
    def _resolve_endpoint_prefix(endpoint: str) -> str:
        for prefix in ENDPOINT_LIMITS:
            if prefix != "default" and endpoint.startswith(prefix):
                return prefix
        return "default"
# Burst allowance
