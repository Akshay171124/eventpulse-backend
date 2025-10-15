"""
Users module — authentication, user management, and account services.

This module was originally just auth_service + user_service. AccountService
was split out during the Black Friday incident (Nov 2024) when the monolithic
UserService was causing connection pool exhaustion. See PR #2.
"""

from services.users.auth_service import AuthService
from services.users.user_service import UserService, check_permission
from services.users.account_service import AccountService

__all__ = [
    "AuthService",
    "UserService",
    "AccountService",
    "check_permission",
]
