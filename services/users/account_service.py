"""
Account & billing service for EventPulse.

NOTE: AccountService was split from UserService during the Black Friday
incident (Nov 2024). See PR #2 for full context.

TL;DR: On Black Friday, /login started returning 503s because the UserService
database connection pool was exhausted. Profile and billing queries were
holding connections open for 2-3 seconds (Stripe API calls inside a DB
transaction — yes, really) while auth queries piled up behind them. Splitting
account/billing into its own service with its OWN connection pool fixed the
immediate issue. We also moved the Stripe calls outside the transaction, but
keeping the pools separate is still the right call for isolation.

This service INTENTIONALLY depends on UserService for RBAC checks.
Do NOT duplicate permission logic here. — @atharvadhumal03
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings
from core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from models.user import User
from models.billing import BillingInfo, Invoice
from models.preferences import UserPreferences
from schemas.account import ProfileUpdate, BillingUpdate, PreferencesUpdate

# This import is critical — AccountService delegates ALL permission checks
# to the shared check_permission function in user_service. If you're
# tempted to inline a quick role check, don't. See the RBAC comments in
# user_service.py for why the logic is more complex than it looks.
from services.users.user_service import check_permission

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Separate connection pool for account/billing queries.
#
# This was the core fix for the Black Friday 503s on /login. The auth
# service was sharing a pool with profile + billing, and Stripe webhook
# processing was holding connections for 2-3s. Under Black Friday load
# (10x normal), the pool was fully exhausted within ~40 seconds.
#
# Pool size is intentionally smaller than the main pool (10 vs 25) because
# account queries are less frequent than auth/event queries. The overflow
# of 5 handles occasional spikes from the billing dashboard.
# -------------------------------------------------------------------------
_account_engine = create_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=5,
    pool_pre_ping=True,
    pool_recycle=1800,
    # Label connections so we can identify them in pg_stat_activity
    connect_args={"application_name": "eventpulse-account-svc"},
)
AccountSessionLocal = sessionmaker(bind=_account_engine)


def get_account_db() -> Session:
    """Get a database session from the account service's connection pool."""
    db = AccountSessionLocal()
    try:
        yield db
    finally:
        db.close()


class AccountService:
    """User profile, billing, and preferences management.

    Split from UserService during the Black Friday incident. This service
    handles the "heavier" user operations (profile updates, billing,
    Stripe interactions) while UserService keeps the lightweight CRUD
    and RBAC logic.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_profile(
        self,
        user_id: int,
        requesting_user_id: int,
        requesting_roles: List[str],
    ) -> Dict[str, Any]:
        """Get full user profile including preferences."""
        has_perm = check_permission(
            requesting_roles,
            "users:read",
            resource_owner_id=user_id,
            requesting_user_id=requesting_user_id,
        )
        if not has_perm:
            raise PermissionDeniedError("Cannot view this profile")

        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise NotFoundError(f"User {user_id} not found")

        prefs = self.db.query(UserPreferences).filter(
            UserPreferences.user_id == user_id
        ).first()

        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "roles": user.roles,
            "is_verified": user.is_verified,
            "created_at": user.created_at.isoformat(),
            "preferences": _serialize_preferences(prefs) if prefs else {},
        }

    def update_profile(
        self,
        user_id: int,
        update_data: ProfileUpdate,
        requesting_user_id: int,
        requesting_roles: List[str],
    ) -> Dict[str, Any]:
        """Update profile fields (display name, avatar, bio).

        This does NOT handle email or role changes — those go through
        UserService.update_user which has extra validation.
        """
        has_perm = check_permission(
            requesting_roles,
            "users:write",
            resource_owner_id=user_id,
            requesting_user_id=requesting_user_id,
        )
        if not has_perm:
            raise PermissionDeniedError("Cannot update this profile")

        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise NotFoundError(f"User {user_id} not found")

        if update_data.display_name is not None:
            if len(update_data.display_name.strip()) < 2:
                raise ValidationError("Display name must be at least 2 characters")
            user.display_name = update_data.display_name.strip()
        if update_data.avatar_url is not None:
            user.avatar_url = update_data.avatar_url
        if update_data.bio is not None:
            user.bio = update_data.bio[:500]  # hard cap, UI enforces 300

        user.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(user)

        logger.info("Profile %d updated by user %d", user_id, requesting_user_id)
        return self.get_profile(user_id, requesting_user_id, requesting_roles)

    def get_billing_info(
        self,
        user_id: int,
        requesting_user_id: int,
        requesting_roles: List[str],
    ) -> Dict[str, Any]:
        """Retrieve billing info and recent invoices.

        NOTE: The actual Stripe API call happens OUTSIDE the DB transaction
        now. We learned this the hard way — see the module docstring.
        """
        has_perm = check_permission(
            requesting_roles,
            "billing:read",
            resource_owner_id=user_id,
            requesting_user_id=requesting_user_id,
        )
        if not has_perm:
            raise PermissionDeniedError("Cannot view billing information")

        billing = self.db.query(BillingInfo).filter(
            BillingInfo.user_id == user_id
        ).first()

        recent_invoices = (
            self.db.query(Invoice)
            .filter(Invoice.user_id == user_id)
            .order_by(Invoice.created_at.desc())
            .limit(10)
            .all()
        )

        return {
            "billing": {
                "stripe_customer_id": billing.stripe_customer_id if billing else None,
                "plan": billing.plan if billing else "free",
                "payment_method_last4": billing.card_last4 if billing else None,
                "billing_email": billing.billing_email if billing else None,
            },
            "invoices": [
                {
                    "id": inv.id,
                    "amount": inv.amount_cents,
                    "currency": inv.currency,
                    "status": inv.status,
                    "created_at": inv.created_at.isoformat(),
                    "pdf_url": inv.pdf_url,
                }
                for inv in recent_invoices
            ],
        }

    def update_billing_info(
        self,
        user_id: int,
        update_data: BillingUpdate,
        requesting_user_id: int,
        requesting_roles: List[str],
    ) -> Dict[str, Any]:
        """Update billing email or payment method reference.

        The actual Stripe payment method update is handled by the payments
        service. This just stores the local reference.
        """
        has_perm = check_permission(
            requesting_roles,
            "billing:write",
            resource_owner_id=user_id,
            requesting_user_id=requesting_user_id,
        )
        if not has_perm:
            raise PermissionDeniedError("Cannot update billing information")

        billing = self.db.query(BillingInfo).filter(
            BillingInfo.user_id == user_id
        ).first()
        if not billing:
            raise NotFoundError("No billing info found. Create a subscription first.")

        if update_data.billing_email is not None:
            billing.billing_email = update_data.billing_email.lower()
        if update_data.card_last4 is not None:
            billing.card_last4 = update_data.card_last4

        billing.updated_at = datetime.now(timezone.utc)
        self.db.commit()

        return self.get_billing_info(user_id, requesting_user_id, requesting_roles)

    def get_preferences(
        self,
        user_id: int,
        requesting_user_id: int,
        requesting_roles: List[str],
    ) -> Dict[str, Any]:
        """Get user notification and display preferences."""
        has_perm = check_permission(
            requesting_roles,
            "users:read",
            resource_owner_id=user_id,
            requesting_user_id=requesting_user_id,
        )
        if not has_perm:
            raise PermissionDeniedError("Cannot view preferences")

        prefs = self.db.query(UserPreferences).filter(
            UserPreferences.user_id == user_id
        ).first()

        if not prefs:
            # Return defaults — preferences row gets created on first update
            return _default_preferences()

        return _serialize_preferences(prefs)


def _serialize_preferences(prefs: UserPreferences) -> Dict[str, Any]:
    return {
        "email_notifications": prefs.email_notifications,
        "push_notifications": prefs.push_notifications,
        "event_reminders": prefs.event_reminders,
        "marketing_emails": prefs.marketing_emails,
        "timezone": prefs.timezone,
        "locale": prefs.locale,
        "theme": prefs.theme,
    }


def _default_preferences() -> Dict[str, Any]:
    return {
        "email_notifications": True,
        "push_notifications": True,
        "event_reminders": True,
        "marketing_emails": False,
        "timezone": "UTC",
        "locale": "en-US",
        "theme": "system",
    }
# Pool monitoring
