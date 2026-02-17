"""FastAPI dependencies for authentication and authorization.

These dependencies are injected into route handlers via Depends().

Key dependencies:
- get_current_user: Resolve JWT claims -> User ORM object (also supports API keys)
- require_role: Assert user has minimum required role
- get_tenant_id: Extract tenant_id UUID from validated claims

Design: JIT user provisioning
  If a user authenticates successfully via JWT but does not yet exist in
  our database, we create them automatically. This avoids the need for a
  separate user provisioning step when using SSO.

API Key support:
  get_current_user checks for API key authentication first (via
  request.state.api_key_raw set by AuthMiddleware) before falling back
  to JWT validation. This allows API keys to work with all existing
  route handlers without modification.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.oidc import TokenValidationError, validate_token
from src.config import Settings, get_settings
from src.database import get_db_session
from src.models.user import User, UserRole

log = structlog.get_logger(__name__)


class AuthenticatedUser:
    """Lightweight container passed to route handlers.

    Combines the ORM User object with the raw JWT claims so that routes
    can access both the database record and any custom claims without
    needing extra queries.
    """

    def __init__(self, user: User, claims: dict) -> None:
        self.user = user
        self.claims = claims

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.user.tenant_id

    @property
    def role(self) -> UserRole:
        return self.user.role

    @property
    def email(self) -> str:
        return self.user.email


async def _extract_and_validate_token(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Extract Bearer token and validate it.

    Returns validated claims dict. Raises HTTP 401 on any failure.
    """
    # Check if middleware already validated the token
    if hasattr(request.state, "auth_claims") and request.state.auth_claims is not None:
        return request.state.auth_claims  # type: ignore[no-any-return]

    # Fallback: validate here (for routes that bypass middleware)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header.removeprefix("Bearer ").strip()
    try:
        return await validate_token(token, settings)
    except TokenValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """Resolve authentication to a User ORM object.

    Checks API key authentication FIRST, before JWT.
    This allows API keys to work transparently with all route handlers.

    Priority:
    1. API key (request.state.api_key_raw set by middleware)
    2. JWT claims (request.state.auth_claims set by middleware)

    Creates the user record if it doesn't exist (JIT provisioning).
    Raises HTTP 401 if tenant_id claim is missing.
    Raises HTTP 403 if the user's account is deactivated.
    """
    # Check API key authentication first (takes precedence over JWT)
    raw_api_key = getattr(request.state, "api_key_raw", None)
    if raw_api_key is not None:
        # Import here to avoid circular dependency at module load time
        from src.auth.api_key_auth import get_current_user_from_api_key
        api_user = await get_current_user_from_api_key(request=request, db=db)
        if api_user is not None:
            return api_user

    # Fall back to JWT authentication
    claims = await _extract_and_validate_token(request=request, settings=settings)

    sub = claims["sub"]
    raw_tenant_id = claims.get("tenant_id")

    if not raw_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT missing tenant_id claim",
        )

    try:
        tenant_id = uuid.UUID(raw_tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid tenant_id in JWT claims",
        )

    # Look up existing user (scoped to tenant for safety)
    stmt = select(User).where(
        User.tenant_id == tenant_id,
        User.external_id == sub,
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        # JIT provisioning: create user on first login
        # Security: JIT-provisioned users always start as VIEWER
        # Role elevation requires explicit admin action (not JWT claims)
        role = UserRole.VIEWER
        claimed_role = claims.get("role")
        if claimed_role and claimed_role != UserRole.VIEWER:
            log.warning(
                "auth.jit_role_claim_ignored",
                claimed=claimed_role,
                assigned="viewer",
                tenant_id=str(tenant_id),
                sub=sub,
            )

        user = User(
            tenant_id=tenant_id,
            external_id=sub,
            email=claims.get("email", f"{sub}@unknown"),
            display_name=claims.get("name"),
            role=role,
        )
        db.add(user)
        await db.flush()  # Get the generated UUID
        log.info("auth.user_provisioned", user_id=str(user.id), tenant_id=str(tenant_id))

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated",
        )

    # Update last login timestamp
    user.last_login_at = datetime.now(UTC)

    return AuthenticatedUser(user=user, claims=claims)


def require_role(*allowed_roles: UserRole) -> Callable:
    """Dependency factory that asserts the current user has one of the allowed roles.

    Usage:
        @router.post("/admin/tenants")
        async def create_tenant(
            current_user: AuthenticatedUser = Depends(require_role(UserRole.ADMIN))
        ):
            ...
    """

    async def _check_role(
        current_user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if current_user.role not in allowed_roles:
            # Security: Don't reveal specific role requirements in error messages
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )
        return current_user

    return _check_role
