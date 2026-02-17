"""WebSocket authentication - validates JWT from query param or first message.

Authentication flow:
1. Check for ?token=<jwt> in query string (fast path for native clients).
2. If absent, wait for first WebSocket message: {"type": "auth", "token": "<jwt>"}.
3. Validate the JWT using the same validate_token() used by HTTP endpoints.
4. Perform JIT user provisioning identical to get_current_user() in dependencies.py.
5. On failure, close the socket with code 4001 (application-level unauthorized)
   and return None.

The WebSocket close code 4001 is in the "application-defined" range (4000-4999)
and signals to clients that re-authentication is required.

Usage:
    @router.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket, db=Depends(get_db_session)):
        user = await authenticate_websocket(websocket, db=db, settings=settings)
        if user is None:
            return  # Socket already closed with 4001
        ...
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser
from src.auth.oidc import TokenValidationError, validate_token
from src.config import Settings
from src.models.user import User, UserRole

log = structlog.get_logger(__name__)

# WebSocket application-level close codes
WS_CLOSE_UNAUTHORIZED = 4001


async def authenticate_websocket(
    websocket: WebSocket,  # noqa: F821 - imported at runtime
    *,
    db: AsyncSession,
    settings: Settings,
) -> AuthenticatedUser | None:
    """Authenticate a WebSocket connection and return the authenticated user.

    Tries two token sources in order:
    1. ?token=<jwt> query parameter (no additional message round-trip)
    2. First WebSocket message: {"type": "auth", "token": "<jwt>"}

    Args:
        websocket: The WebSocket connection to authenticate.
        db: Database session for user lookup / JIT provisioning.
        settings: Application settings for JWT validation.

    Returns:
        AuthenticatedUser on success, None on failure (socket is closed).
    """
    # ------------------------------------------------------------------ #
    # 1. Try query param token
    # ------------------------------------------------------------------ #
    token = websocket.query_params.get("token")

    # ------------------------------------------------------------------ #
    # 2. Fallback: wait for first auth message
    # ------------------------------------------------------------------ #
    if not token:
        try:
            first_msg = await websocket.receive_json()
        except Exception as exc:
            log.warning("ws.auth_receive_failed", error=str(exc))
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
            return None

        if not isinstance(first_msg, dict) or first_msg.get("type") != "auth":
            log.warning(
                "ws.auth_invalid_first_message",
                msg_type=first_msg.get("type") if isinstance(first_msg, dict) else None,
            )
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
            return None

        token = first_msg.get("token", "")

    # ------------------------------------------------------------------ #
    # 3. Validate JWT
    # ------------------------------------------------------------------ #
    if not token:
        log.warning("ws.auth_missing_token")
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return None

    try:
        claims = await validate_token(token, settings)
    except TokenValidationError as exc:
        log.warning("ws.auth_token_invalid", error=str(exc))
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return None

    # ------------------------------------------------------------------ #
    # 4. Extract required claims
    # ------------------------------------------------------------------ #
    sub = claims.get("sub")
    raw_tenant_id = claims.get("tenant_id")

    if not sub or not raw_tenant_id:
        log.warning("ws.auth_missing_claims", claims=list(claims.keys()))
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return None

    try:
        tenant_id = uuid.UUID(raw_tenant_id)
    except ValueError:
        log.warning("ws.auth_invalid_tenant_id", raw=raw_tenant_id)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return None

    # ------------------------------------------------------------------ #
    # 5. Resolve or JIT-provision user (mirrors dependencies.get_current_user)
    # ------------------------------------------------------------------ #
    stmt = select(User).where(
        User.tenant_id == tenant_id,
        User.external_id == sub,
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        # JIT provisioning: new users always start as VIEWER (per security policy)
        user = User(
            tenant_id=tenant_id,
            external_id=sub,
            email=claims.get("email", f"{sub}@unknown"),
            display_name=claims.get("name"),
            role=UserRole.VIEWER,
            is_active=True,
        )
        db.add(user)
        await db.flush()
        log.info("ws.auth_user_provisioned", user_id=str(user.id), tenant_id=str(tenant_id))

    if not user.is_active:
        log.warning("ws.auth_user_deactivated", user_id=str(user.id))
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return None

    # Update last login
    user.last_login_at = datetime.now(UTC)

    log.info(
        "ws.auth_success",
        user_id=str(user.id),
        tenant_id=str(tenant_id),
    )

    return AuthenticatedUser(user=user, claims=claims)
