"""JWT validation middleware.

This Starlette middleware runs before any route handler. It:
1. Checks for API key in X-API-Key header or Bearer eap_ token
2. If API key detected, marks request.state.api_key_raw and skips JWT validation
3. Extracts the Bearer token from the Authorization header (for JWT)
4. Validates the token via the OIDC module
5. Injects the validated claims into request.state

Routes that need authentication use the FastAPI dependencies in
dependencies.py (get_current_user, require_role). This middleware
simply makes the raw claims available.

We deliberately do NOT raise HTTP 401 here for missing tokens because
some routes (health, docs) are public. The FastAPI dependencies enforce
authentication at the route level.

API Key support:
- X-API-Key header takes precedence over Authorization header
- Authorization: Bearer eap_... is recognized as an API key
- Both set request.state.api_key_raw instead of request.state.auth_claims
"""

from __future__ import annotations

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from src.auth.oidc import TokenValidationError, validate_token
from src.config import get_settings

log = structlog.get_logger(__name__)

# Routes that are always public - skip token extraction entirely
_PUBLIC_PREFIXES = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
)

# API key prefix for identification
_API_KEY_PREFIX = "eap_"


def _extract_api_key(request: Request) -> str | None:
    """Extract API key from request, if present.

    Checks:
    1. X-API-Key header
    2. Authorization: Bearer eap_... header

    Args:
        request: Incoming HTTP request

    Returns:
        Raw API key string or None
    """
    # X-API-Key header takes priority
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return api_key

    # Check Authorization header for eap_ prefix
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith(f"Bearer {_API_KEY_PREFIX}"):
        return auth_header.removeprefix("Bearer ").strip()

    return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Extract and validate JWT or detect API key, inject into request.state.

    On JWT success: request.state.auth_claims is set to the claims dict.
    On API key detected: request.state.api_key_raw is set, auth_claims is None.
    On failure or missing token: request.state.auth_claims is None, api_key_raw is None.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()

        # Initialize state
        request.state.auth_claims = None
        request.state.api_key_raw = None

        # Short-circuit for public routes
        if any(request.url.path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
            return await call_next(request)

        # Check for API key BEFORE JWT validation (API keys take precedence)
        raw_api_key = _extract_api_key(request)
        if raw_api_key is not None:
            # API key detected - store for downstream handling
            # Actual validation happens in the API key auth dependency
            request.state.api_key_raw = raw_api_key
            log.debug(
                "auth.api_key_detected",
                prefix=raw_api_key[:8] if len(raw_api_key) >= 8 else "invalid",
            )
            # Skip JWT validation - API key auth handles this request
            return await call_next(request)

        # Standard JWT validation path
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            # No auth header - pass through (dependency will reject if needed)
            return await call_next(request)

        token = auth_header.removeprefix("Bearer ").strip()
        try:
            claims = await validate_token(token, settings)
            request.state.auth_claims = claims
            log.debug(
                "auth.token_validated",
                sub=claims.get("sub"),
                tenant_id=claims.get("tenant_id"),
            )
        except TokenValidationError as exc:
            log.warning("auth.token_invalid", error=str(exc))
            request.state.auth_claims = None

        return await call_next(request)
