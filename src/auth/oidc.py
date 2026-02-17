"""OIDC discovery and token validation.

In production mode:
- Fetches JWKS from the OIDC discovery document at startup
- Validates JWT signature against the public key set
- Caches JWKS with a TTL (5 minutes) to avoid hammering the IdP

In dev mode (ENVIRONMENT=dev):
- Validates JWTs using the symmetric DEV_JWT_SECRET
- Skips JWKS fetch entirely
- Logs a loud warning on startup

Required JWT claims:
  - sub: string - external user ID (maps to users.external_id)
  - tenant_id: string UUID - the tenant this token grants access to
  - role: string - user role (admin/operator/viewer)
  - exp: int - expiration timestamp
  - aud: string|list - must include OIDC_AUDIENCE

Optional claims:
  - email: string
  - name: string
"""

from __future__ import annotations

import json
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx
import jwt
import structlog
from jwt.exceptions import DecodeError, InvalidTokenError

from src.config import Settings

log = structlog.get_logger(__name__)

# JWKS cache: dict of kid -> key material, plus a fetch timestamp
_jwks_cache: dict[str, Any] = {}
_jwks_fetched_at: float = 0.0
_JWKS_TTL_SECONDS = 300  # Refresh JWKS every 5 minutes


async def _fetch_jwks(issuer_url: str) -> dict[str, Any]:
    """Fetch JWKS from the OIDC discovery endpoint via HTTP."""
    discovery_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10.0) as client:
        discovery = await client.get(discovery_url)
        discovery.raise_for_status()
        jwks_uri = discovery.json()["jwks_uri"]

        jwks_response = await client.get(jwks_uri)
        jwks_response.raise_for_status()
        return jwks_response.json()  # type: ignore[no-any-return]


def _load_local_jwks(path: str) -> dict[str, Any]:
    """Load JWKS from a local file (air-gapped / offline mode).

    Args:
        path: Filesystem path to a JWKS JSON file.

    Returns:
        Parsed JWKS dict, identical in structure to what the IdP would return.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not valid JSON or missing 'keys'.
    """
    jwks_path = Path(path)
    if not jwks_path.exists():
        raise FileNotFoundError(f"JWKS local file not found: {path}")

    log.warning(
        "oidc.local_jwks_mode_active",
        message=(
            "Reading JWKS from local file — NOT fetching from IdP. "
            "Ensure the file is kept up-to-date when keys are rotated."
        ),
        jwks_local_path=path,
    )

    raw = json.loads(jwks_path.read_text(encoding="utf-8"))
    if "keys" not in raw:
        raise ValueError(f"JWKS file at {path!r} is missing the 'keys' array")
    return raw  # type: ignore[return-value]


async def _get_jwks(settings: Settings) -> dict[str, Any]:
    """Return cached JWKS or fetch/load fresh copy.

    If settings.jwks_local_path is set, reads from a local file instead of
    making HTTP requests to the IdP (useful for air-gapped environments).
    The TTL-based cache still applies so the file is not re-read on every request.
    """
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    if not _jwks_cache or (now - _jwks_fetched_at) > _JWKS_TTL_SECONDS:
        if settings.jwks_local_path:
            raw = _load_local_jwks(settings.jwks_local_path)
        else:
            raw = await _fetch_jwks(settings.oidc_issuer_url)
        _jwks_cache = {key["kid"]: key for key in raw.get("keys", [])}
        _jwks_fetched_at = now
        log.info("oidc.jwks_refreshed", key_count=len(_jwks_cache))
    return _jwks_cache


class TokenValidationError(Exception):
    """Raised when a JWT cannot be validated."""


async def validate_token(token: str, settings: Settings) -> dict[str, Any]:
    """Validate a JWT and return its claims.

    Raises TokenValidationError if the token is invalid, expired, or
    has incorrect audience/issuer.
    """
    if settings.is_dev:
        return _validate_dev_token(token, settings)

    # Production: validate against OIDC JWKS
    try:
        # Decode header to get kid without verification
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
    except (DecodeError, InvalidTokenError) as exc:
        raise TokenValidationError(f"Cannot decode token header: {exc}") from exc

    jwks = await _get_jwks(settings)

    if kid and kid in jwks:
        key_data = jwks[kid]
    elif jwks:
        # Fallback: try all keys (useful for RS256 with a single key but no kid)
        key_data = next(iter(jwks.values()))
    else:
        raise TokenValidationError("No JWKS keys available")

    try:
        # PyJWT uses PyJWK to construct key from JWKS key data
        signing_key = jwt.PyJWK(key_data)
    except (DecodeError, InvalidTokenError) as exc:
        raise TokenValidationError(f"Cannot construct JWK: {exc}") from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer_url,
            options={"verify_exp": True, "verify_iat": True},
        )
    except (DecodeError, InvalidTokenError) as exc:
        raise TokenValidationError(f"Token validation failed: {exc}") from exc

    _assert_required_claims(claims)
    return claims


_dev_mode_warned = False


def _validate_dev_token(token: str, settings: Settings) -> dict[str, Any]:
    """Validate JWT using symmetric secret (dev only)."""
    global _dev_mode_warned
    if not _dev_mode_warned:
        log.warning(
            "oidc.dev_mode_validation",
            message="Using symmetric JWT secret - NOT for production",
        )
        _dev_mode_warned = True
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.dev_jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=settings.oidc_audience,
            options={"verify_exp": True, "verify_aud": True},
        )
    except (DecodeError, InvalidTokenError) as exc:
        raise TokenValidationError(f"Dev token validation failed: {exc}") from exc

    _assert_required_claims(claims)
    return claims


def _assert_required_claims(claims: dict[str, Any]) -> None:
    """Raise TokenValidationError if required claims are missing."""
    required = ("sub", "tenant_id", "role")
    missing = [c for c in required if not claims.get(c)]
    if missing:
        raise TokenValidationError(f"Missing required JWT claims: {missing}")


def create_dev_token(
    *,
    sub: str,
    tenant_id: str,
    role: str = "viewer",
    email: str = "",
    secret: str,
    expires_in: int = 3600,
) -> str:
    """Create a dev JWT for testing purposes.

    Never call this in production code.
    """
    import uuid
    from datetime import datetime

    now = int(datetime.now(UTC).timestamp())
    payload = {
        "sub": sub,
        "tenant_id": tenant_id,
        "role": role,
        "email": email,
        "aud": "enterprise-agents-api",
        "iat": now,
        "exp": now + expires_in,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")
