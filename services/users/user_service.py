"""
User management and RBAC service for EventPulse.

Handles user CRUD, search, and the centralized permission checking logic
that other services depend on (especially AccountService).

Authors: @atharvadhumal03, @Akshay171124
"""

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from core.config import settings
from core.exceptions import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from models.user import User
from schemas.user import UserUpdate, UserFilter

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Role hierarchy for RBAC
#
# This took about 2 weeks to get right. The tricky parts:
#
#  1) "organizer" can manage their OWN events and the attendees of those
#     events, but NOT other organizers' events. The permission check has
#     to be context-aware (i.e., which event are we talking about?).
#
#  2) "moderator" was added later for community features. A moderator can
#     manage users in their assigned community but can't touch billing or
#     event creation. We originally tried making moderator a subset of
#     organizer, but the permissions don't actually nest cleanly.
#
#  3) "super_admin" bypasses all checks. We considered removing this and
#     using fine-grained permissions only, but for a team our size it's
#     not worth the complexity. Revisit if the team grows past ~20.
#
# The ROLE_HIERARCHY dict maps each role to the set of permissions it has.
# check_permission() below is the ONE place that evaluates this.
# DO NOT add ad-hoc role checks elsewhere — always call check_permission.
# -------------------------------------------------------------------------

ROLE_HIERARCHY: Dict[str, set] = {
    "super_admin": {
        "users:read", "users:write", "users:delete", "users:admin",
        "events:read", "events:write", "events:delete", "events:admin",
        "billing:read", "billing:write",
        "analytics:read", "analytics:export",
        "community:read", "community:moderate",
    },
    "admin": {
        "users:read", "users:write", "users:delete",
        "events:read", "events:write", "events:delete",
        "billing:read", "billing:write",
        "analytics:read", "analytics:export",
    },
    "organizer": {
        "users:read",
        "events:read", "events:write", "events:delete",
        "billing:read",
        "analytics:read",
    },
    "moderator": {
        "users:read",
        "events:read",
        "community:read", "community:moderate",
    },
    "attendee": {
        "users:read",
        "events:read",
        "billing:read",
    },
}


def check_permission(
    user_roles: List[str],
    required_permission: str,
    *,
    resource_owner_id: Optional[int] = None,
    requesting_user_id: Optional[int] = None,
) -> bool:
    """Central permission check. Used by UserService, AccountService, and route guards.

    Returns True if the user has the required permission, False otherwise.

    The resource_owner_id / requesting_user_id kwargs handle the "users can
    edit their own profile" case. If both are provided and they match, we
    grant the permission even if the role wouldn't normally allow it —
    but ONLY for read/write, never for delete or admin operations.

    IMPORTANT: AccountService imports this function directly. If you change
    the signature, update account_service.py as well or things will break
    in subtle ways (ask me how I know — @atharvadhumal03).
    """
    # Super admin short-circuit
    if "super_admin" in user_roles:
        return True

    # Self-access rule: users can read/write their own resources
    # but cannot self-delete or self-promote to admin
    if (
        resource_owner_id is not None
        and requesting_user_id is not None
        and resource_owner_id == requesting_user_id
    ):
        action = required_permission.split(":")[-1] if ":" in required_permission else ""
        if action in ("read", "write"):
            return True

    # Standard role-based check
    for role in user_roles:
        role_perms = ROLE_HIERARCHY.get(role, set())
        if required_permission in role_perms:
            return True

    return False


class UserService:
    """User CRUD and management operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_user(self, user_id: int, requesting_roles: List[str]) -> User:
        if not check_permission(requesting_roles, "users:read"):
            raise PermissionDeniedError("Insufficient permissions to view user")

        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise NotFoundError(f"User {user_id} not found")
        return user

    def update_user(
        self,
        user_id: int,
        update_data: UserUpdate,
        requesting_user_id: int,
        requesting_roles: List[str],
    ) -> User:
        """Update user fields. Requires users:write or self-access."""
        has_perm = check_permission(
            requesting_roles,
            "users:write",
            resource_owner_id=user_id,
            requesting_user_id=requesting_user_id,
        )
        if not has_perm:
            raise PermissionDeniedError("Cannot update this user")

        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise NotFoundError(f"User {user_id} not found")

        # Only admins can change roles — this was a bug for 3 days in staging
        # where organizers could promote themselves to admin via the profile
        # update endpoint. Caught during pen-test, thankfully not in prod.
        if update_data.roles is not None:
            if not check_permission(requesting_roles, "users:admin"):
                raise PermissionDeniedError("Only admins can modify roles")
            user.roles = update_data.roles

        if update_data.display_name is not None:
            user.display_name = update_data.display_name
        if update_data.email is not None:
            # Check uniqueness
            conflict = self.db.query(User).filter(
                User.email == update_data.email.lower(),
                User.id != user_id,
            ).first()
            if conflict:
                raise ValidationError("Email already in use")
            user.email = update_data.email.lower()
            user.is_verified = False  # re-verify on email change

        user.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(user)
        logger.info("User %d updated by user %d", user_id, requesting_user_id)
        return user

    def list_users(
        self,
        filters: UserFilter,
        requesting_roles: List[str],
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """Paginated user listing with optional filters."""
        if not check_permission(requesting_roles, "users:read"):
            raise PermissionDeniedError("Insufficient permissions")

        query = self.db.query(User)

        if filters.search:
            term = f"%{filters.search}%"
            query = query.filter(
                or_(
                    User.display_name.ilike(term),
                    User.email.ilike(term),
                )
            )
        if filters.role:
            # roles is stored as a JSON array in Postgres — this uses
            # the @> containment operator via SQLAlchemy's .contains()
            query = query.filter(User.roles.contains([filters.role]))
        if filters.is_active is not None:
            query = query.filter(User.is_active == filters.is_active)

        total = query.count()
        users = (
            query
            .order_by(User.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        return {
            "users": users,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
        }

    def deactivate_user(
        self,
        user_id: int,
        requesting_user_id: int,
        requesting_roles: List[str],
    ) -> User:
        """Soft-delete: mark user as inactive. Requires users:delete.

        We don't hard-delete because of foreign key references in the
        events and payments tables. The nightly cleanup job handles
        PII removal for users inactive > 90 days (GDPR).
        """
        if not check_permission(requesting_roles, "users:delete"):
            raise PermissionDeniedError("Cannot deactivate users")

        if user_id == requesting_user_id:
            raise ValidationError(
                "Cannot deactivate your own account via this endpoint. "
                "Use /account/close instead."
            )

        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise NotFoundError(f"User {user_id} not found")

        user.is_active = False
        user.deactivated_at = datetime.now(timezone.utc)
        self.db.commit()
        logger.warning(
            "User %d deactivated by %d", user_id, requesting_user_id
        )
        return user
# Permission cache
