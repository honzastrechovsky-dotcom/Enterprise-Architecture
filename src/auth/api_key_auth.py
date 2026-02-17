"""API Key authentication module.

Provides API key validation as an alternative to JWT authentication.

The middleware (middleware.py) detects API keys and stores the raw key in
request.state.api_key_raw. This module validates the key and creates a
synthetic AuthenticatedUser.

API keys can be provided via:
1. X-API-Key header
2. Authorization: Bearer eap_... header (detected by middleware)

Flow:
1. AuthMiddleware detects eap_ key -> stores in request.state.api_key_raw
2. get_current_user_from_api_key reads state.api_key_raw and validates
3. Returns AuthenticatedUser with API key claims
4. Falls through to JWT if no API key present
"""

from __future__ import annotations

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser
from src.database import get_db_session
from src.models.api_key import APIKey
from src.models.user import User, UserRole
from src.services.api_keys import APIKeyService, InvalidAPIKeyError

log = structlog.get_logger(__name__)


async def get_current_user_from_api_key(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> AuthenticatedUser | None:
    """Authenticate using API key if present in request state.

    The middleware pre-processes the request and stores the raw API key in
    request.state.api_key_raw if it detects an API key header.

    This dependency reads that state and validates the key.

    Args:
        request: FastAPI request object (API key in request.state.api_key_raw)
        db: Database session

    Returns:
        AuthenticatedUser if valid API key found, None otherwise

    Raises:
        HTTPException: If API key is present but invalid/expired/revoked
    """
    # Check if middleware detected an API key
    raw_key = getattr(request.state, "api_key_raw", None)

    if raw_key is None:
        # No API key detected by middleware
        return None

    # Validate the API key
    service = APIKeyService(db)
    try:
        api_key = await service.validate_key(raw_key)
    except InvalidAPIKeyError as exc:
        log.warning(
            "api_key.auth_failed",
            error=str(exc),
            key_prefix=raw_key[:8] if len(raw_key) >= 8 else "invalid",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Update last used timestamp (non-blocking - don't await flush separately)
    await service.update_last_used(api_key.id)

    # Create synthetic AuthenticatedUser from API key
    auth_user = create_authenticated_user_from_api_key(api_key)

    log.info(
        "api_key.auth_success",
        key_id=str(api_key.id),
        tenant_id=str(api_key.tenant_id),
        name=api_key.name,
        scopes=api_key.scopes,
    )

    return auth_user


def create_authenticated_user_from_api_key(api_key: APIKey) -> AuthenticatedUser:
    """Create a synthetic AuthenticatedUser from an API key.

    This allows API keys to work seamlessly with existing authorization logic
    that expects AuthenticatedUser objects.

    The synthetic user uses the API key ID as the user ID, with the tenant
    scoped correctly. The role is set to OPERATOR as a safe default.

    Args:
        api_key: Validated APIKey model

    Returns:
        AuthenticatedUser instance with synthetic User

    Security notes:
        - Synthetic user ID = API key ID (for tracing/auditing)
        - Role defaults to OPERATOR (read + write, but not admin)
        - Scopes stored in claims for fine-grained access control
        - API key admin operations still require JWT admin token
    """
    # Create a synthetic User object representing this API key as an actor
    # This user does not exist in the DB - it's a transient authorization entity
    synthetic_user = User(
        id=api_key.id,  # Use API key ID as user ID for auditability
        tenant_id=api_key.tenant_id,
        external_id=f"api_key:{api_key.id}",
        email=f"api-key+{str(api_key.id)[:8]}@system.internal",
        display_name=f"API Key: {api_key.name}",
        role=UserRole.OPERATOR,  # API keys operate as OPERATOR by default
        is_active=True,
    )

    # Create claims dict with full API key metadata for authorization checks
    claims = {
        "sub": f"api_key:{api_key.id}",
        "tenant_id": str(api_key.tenant_id),
        "api_key_id": str(api_key.id),
        "api_key_name": api_key.name,
        "api_key_scopes": api_key.scopes,
        "auth_method": "api_key",
    }

    return AuthenticatedUser(user=synthetic_user, claims=claims)


def require_scope(*required_scopes: str):
    """Dependency factory that enforces API key scopes.

    For API key authentication, checks that the key has at least one of
    the required scopes. JWT-authenticated users (who have no api_key_scopes
    claim) bypass this check entirely.

    Args:
        required_scopes: One or more scopes, any of which satisfies the requirement

    Returns:
        FastAPI dependency function

    Example:
        @router.post("/chat")
        async def chat(
            current_user: AuthenticatedUser = Depends(get_current_user),
            _scope: AuthenticatedUser = Depends(require_scope("chat")),
        ):
            ...
    """

    async def _check_scope(
        request: Request,
        current_user: AuthenticatedUser = Depends(get_current_user_from_api_key),
    ) -> None:
        """Check scope on the current API key if applicable."""
        if current_user is None:
            # Not an API key request - JWT users have full scope access
            return

        # Get API key scopes from claims
        api_key_scopes = current_user.claims.get("api_key_scopes", [])

        if not any(scope in api_key_scopes for scope in required_scopes):
            log.warning(
                "api_key.scope_denied",
                required_scopes=list(required_scopes),
                api_key_scopes=api_key_scopes,
                key_id=current_user.claims.get("api_key_id"),
                tenant_id=str(current_user.tenant_id),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key missing required scope. Required: {', '.join(required_scopes)}",
            )

    return _check_scope


def is_api_key_authenticated(user: AuthenticatedUser) -> bool:
    """Check if the user was authenticated via API key (not JWT).

    Args:
        user: AuthenticatedUser from dependency injection

    Returns:
        True if authenticated via API key
    """
    return user.claims.get("auth_method") == "api_key"
