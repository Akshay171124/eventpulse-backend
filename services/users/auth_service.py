"""
Authentication service for EventPulse.

Handles login, registration, token management, SSO, and session tracking.
Original implementation by @atharvadhumal03. MFA and rate limiting added
later by @Akshay171124 after the security audit (Jan 2025).
"""

import hashlib
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple

import jwt
import redis
import httpx
from sqlalchemy.orm import Session

from core.config import settings
from core.exceptions import (
    AuthenticationError,
    RateLimitExceeded,
    MFARequiredError,
)
from models.user import User
from schemas.auth import TokenPair, LoginRequest, RegisterRequest

logger = logging.getLogger(__name__)

# Redis connection for sessions + rate limiting
_redis: redis.Redis = redis.from_url(
    settings.REDIS_URL, decode_responses=True
)

# Rate limiting config — added by @Akshay171124 after we found someone
# brute-forcing the /login endpoint in prod. 5 attempts per 15-min window
# felt reasonable; bump if legitimate users start complaining.
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW = 900  # seconds


class AuthService:
    """Core authentication service."""

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Rate limiting (added by @Akshay171124)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_rate_limit(identifier: str) -> None:
        """Check login rate limit for a given identifier (email or IP).

        We key on both email AND IP separately so that:
        - An attacker can't lock out a real user by spamming their email
          from a single IP (IP limit hits first)
        - Credential stuffing across many emails from one IP still gets caught
        """
        key = f"rate:login:{identifier}"
        current = _redis.get(key)
        if current and int(current) >= LOGIN_RATE_LIMIT:
            ttl = _redis.ttl(key)
            raise RateLimitExceeded(
                f"Too many login attempts. Try again in {ttl} seconds."
            )
        pipe = _redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, LOGIN_RATE_WINDOW)
        pipe.execute()

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_tokens(user_id: int, roles: list[str]) -> TokenPair:
        now = datetime.now(timezone.utc)
        access_payload = {
            "sub": str(user_id),
            "roles": roles,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MIN),
        }
        refresh_payload = {
            "sub": str(user_id),
            "type": "refresh",
            "iat": now,
            "exp": now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        }
        access_token = jwt.encode(
            access_payload, settings.JWT_SECRET, algorithm="HS256"
        )
        refresh_token = jwt.encode(
            refresh_payload, settings.JWT_SECRET, algorithm="HS256"
        )
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MIN * 60,
        )

    @staticmethod
    def verify_token(token: str) -> Dict[str, Any]:
        """Decode and verify a JWT. Raises AuthenticationError on failure."""
        try:
            payload = jwt.decode(
                token, settings.JWT_SECRET, algorithms=["HS256"]
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise AuthenticationError("Token has expired")
        except jwt.InvalidTokenError:
            raise AuthenticationError("Invalid token")

    # ------------------------------------------------------------------
    # Session management (Redis-backed)
    # ------------------------------------------------------------------

    @staticmethod
    def _create_session(user_id: int, token_jti: str, meta: Dict) -> None:
        session_key = f"session:{user_id}:{token_jti}"
        _redis.hset(session_key, mapping={
            "user_id": str(user_id),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ip": meta.get("ip", "unknown"),
            "user_agent": meta.get("user_agent", ""),
        })
        _redis.expire(session_key, settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400)

    @staticmethod
    def _revoke_session(user_id: int, token_jti: str) -> None:
        _redis.delete(f"session:{user_id}:{token_jti}")

    # ------------------------------------------------------------------
    # Core auth flows
    # ------------------------------------------------------------------

    def register(self, request: RegisterRequest) -> TokenPair:
        existing = self.db.query(User).filter(
            User.email == request.email.lower()
        ).first()
        if existing:
            raise AuthenticationError("Email already registered")

        hashed_pw = hashlib.pbkdf2_hmac(
            "sha256",
            request.password.encode(),
            settings.PASSWORD_SALT.encode(),
            iterations=100_000,
        ).hex()

        user = User(
            email=request.email.lower(),
            password_hash=hashed_pw,
            display_name=request.display_name,
            roles=["attendee"],
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)

        logger.info("New user registered: %s (id=%d)", user.email, user.id)
        return self._generate_tokens(user.id, user.roles)

    def login(
        self, request: LoginRequest, client_meta: Dict[str, str]
    ) -> Tuple[TokenPair, bool]:
        """Authenticate user. Returns (tokens, mfa_required).

        If mfa_required is True, the access token is a short-lived
        partial token that only grants access to /auth/mfa/verify.
        """
        # Rate limit on both email and IP
        self._check_rate_limit(f"email:{request.email.lower()}")
        self._check_rate_limit(f"ip:{client_meta.get('ip', 'unknown')}")

        user = self.db.query(User).filter(
            User.email == request.email.lower()
        ).first()
        if not user or not user.is_active:
            raise AuthenticationError("Invalid email or password")

        hashed = hashlib.pbkdf2_hmac(
            "sha256",
            request.password.encode(),
            settings.PASSWORD_SALT.encode(),
            iterations=100_000,
        ).hex()
        if hashed != user.password_hash:
            raise AuthenticationError("Invalid email or password")

        # MFA check — added by @Akshay171124
        # If user has MFA enabled, return a partial token that only works
        # for the /auth/mfa/verify endpoint. The frontend handles the rest.
        if user.mfa_enabled:
            partial = self._generate_partial_mfa_token(user.id)
            return partial, True

        tokens = self._generate_tokens(user.id, user.roles)
        self._create_session(user.id, tokens.access_token[:16], client_meta)
        return tokens, False

    def verify_mfa(self, partial_token: str, totp_code: str) -> TokenPair:
        """Verify MFA TOTP code and issue full tokens.

        Added by @Akshay171124 — uses pyotp under the hood.
        """
        import pyotp

        payload = self.verify_token(partial_token)
        if payload.get("type") != "mfa_partial":
            raise AuthenticationError("Invalid MFA token")

        user = self.db.query(User).get(int(payload["sub"]))
        if not user or not user.mfa_secret:
            raise AuthenticationError("MFA not configured")

        totp = pyotp.TOTP(user.mfa_secret)
        if not totp.verify(totp_code, valid_window=1):
            raise AuthenticationError("Invalid MFA code")

        return self._generate_tokens(user.id, user.roles)

    def _generate_partial_mfa_token(self, user_id: int) -> TokenPair:
        """Generate a short-lived token that only permits MFA verification."""
        now = datetime.now(timezone.utc)
        partial_payload = {
            "sub": str(user_id),
            "type": "mfa_partial",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        token = jwt.encode(
            partial_payload, settings.JWT_SECRET, algorithm="HS256"
        )
        return TokenPair(
            access_token=token, refresh_token="", expires_in=300
        )

    def refresh_token(self, refresh_tok: str) -> TokenPair:
        payload = self.verify_token(refresh_tok)
        if payload.get("type") != "refresh":
            raise AuthenticationError("Invalid refresh token")

        user = self.db.query(User).get(int(payload["sub"]))
        if not user or not user.is_active:
            raise AuthenticationError("User not found or deactivated")

        return self._generate_tokens(user.id, user.roles)

    def logout(self, token: str) -> None:
        payload = self.verify_token(token)
        user_id = int(payload["sub"])
        self._revoke_session(user_id, token[:16])
        # Also add to a short-lived blacklist so the access token can't
        # be reused for the remaining TTL
        _redis.setex(
            f"blacklist:{token[:32]}",
            settings.ACCESS_TOKEN_EXPIRE_MIN * 60,
            "1",
        )

    # ------------------------------------------------------------------
    # SSO / Okta integration
    # ------------------------------------------------------------------

    async def sso_authenticate(self, okta_token: str) -> TokenPair:
        """Authenticate via Okta SSO. Used by enterprise customers.

        NOTE(@atharvadhumal03): Okta sometimes returns the email domain in
        uppercase (e.g. "user@EVENTPULSE.COM"). We lowercase the whole email
        before lookup. This bit us in prod for ~2 hours before we noticed.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.OKTA_ISSUER}/oauth2/v1/userinfo",
                headers={"Authorization": f"Bearer {okta_token}"},
            )
            resp.raise_for_status()
            profile = resp.json()

        email = profile["email"].lower()  # see NOTE above

        # Internal @eventpulse.com accounts skip email verification —
        # they're already verified via Okta. This saves a round-trip
        # to the verification service and avoids the "verify your email"
        # banner in the UI that confused our own team.
        skip_verification = email.endswith("@eventpulse.com")

        user = self.db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                email=email,
                display_name=profile.get("name", email.split("@")[0]),
                password_hash="",  # SSO users don't have passwords
                roles=["attendee"],
                is_verified=skip_verification,
                sso_provider="okta",
            )
            self.db.add(user)
            self.db.commit()
            self.db.refresh(user)

        return self._generate_tokens(user.id, user.roles)
# Session cleanup
# Token rotation
