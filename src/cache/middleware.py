"""Cache middleware for FastAPI.

Intercepts GET requests and serves cached responses when available,
injecting appropriate X-Cache headers to signal cache state to clients.

Caching rules:
- Only GET requests are cached (POST/PUT/DELETE always bypass)
- Only 200 OK responses are stored (errors are never cached)
- Cache is per (tenant_id, path+query_string), NOT per individual user
  This is safe because all users within a tenant share the same access
  model. Auth is still enforced - the middleware does NOT skip auth.
- Responses containing Set-Cookie headers are NOT cached
- The tenant_id is extracted from the validated JWT claims stored in
  request.state.auth_claims (set by the upstream auth middleware)

Headers added to responses:
- X-Cache: HIT   - served from cache
- X-Cache: MISS  - not in cache (response was processed and stored)
- X-Cache: SKIP  - caching was not applicable (non-GET, non-200, etc.)
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from src.cache.backend import CacheBackend

log = structlog.get_logger(__name__)

_CACHE_RESPONSE_NS = "http"
_DEFAULT_HTTP_TTL = 300  # 5 minutes for HTTP-level caching


def _http_cache_key(tenant_id: str, path: str, query_string: str) -> str:
    """Build deterministic cache key for an HTTP GET request."""
    raw = f"{tenant_id}:{path}:{query_string}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"{_CACHE_RESPONSE_NS}:{digest}"


def _should_cache_request(request: Request) -> bool:
    """Return True if this request is eligible for caching."""
    if request.method != "GET":
        return False
    # Skip health checks and cache management endpoints
    skip_prefixes = ("/health", "/api/v1/cache")
    for prefix in skip_prefixes:
        if request.url.path.startswith(prefix):
            return False
    return True


def _extract_tenant_id(request: Request) -> str | None:
    """Extract tenant_id from auth claims stored in request.state."""
    claims = getattr(request.state, "auth_claims", None)
    if claims and isinstance(claims, dict):
        return str(claims.get("tenant_id", ""))
    return None


class CacheMiddleware(BaseHTTPMiddleware):
    """FastAPI/Starlette middleware that caches GET responses per tenant.

    Designed to sit in the middleware stack after authentication middleware
    so that request.state.auth_claims is already populated when this runs.
    """

    def __init__(
        self,
        app: ASGIApp,
        backend: CacheBackend,
        ttl: int = _DEFAULT_HTTP_TTL,
        enabled: bool = True,
    ) -> None:
        super().__init__(app)
        self._backend = backend
        self._ttl = ttl
        self._enabled = enabled

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self._enabled or not _should_cache_request(request):
            response = await call_next(request)
            response.headers["X-Cache"] = "SKIP"
            return response

        tenant_id = _extract_tenant_id(request)
        if not tenant_id:
            # No tenant context - cannot safely cache (might serve wrong data)
            response = await call_next(request)
            response.headers["X-Cache"] = "SKIP"
            return response

        cache_key = _http_cache_key(
            tenant_id=tenant_id,
            path=request.url.path,
            query_string=str(request.url.query),
        )

        # --- Cache lookup ---
        cached_data = await self._backend.get(cache_key)
        if cached_data is not None:
            log.debug(
                "cache.middleware.hit",
                path=request.url.path,
                tenant_id=tenant_id[:8],
            )
            return JSONResponse(
                content=cached_data.get("body"),
                status_code=cached_data.get("status_code", 200),
                headers={"X-Cache": "HIT"},
            )

        # --- Cache miss - process request ---
        response = await call_next(request)
        response.headers["X-Cache"] = "MISS"

        # Only cache successful responses with JSON content
        if (
            response.status_code == 200
            and "Set-Cookie" not in response.headers
            and "application/json" in response.headers.get("content-type", "")
        ):
            try:
                # Read and decode body for caching
                body_bytes = b""
                async for chunk in response.body_iterator:
                    body_bytes += chunk

                body_json = json.loads(body_bytes)
                await self._backend.set(
                    cache_key,
                    {"body": body_json, "status_code": 200},
                    self._ttl,
                )
                log.debug(
                    "cache.middleware.stored",
                    path=request.url.path,
                    tenant_id=tenant_id[:8],
                    ttl=self._ttl,
                )
                # Return a new response from the consumed body
                return JSONResponse(
                    content=body_json,
                    status_code=200,
                    headers=dict(response.headers),
                )
            except Exception as exc:
                log.warning(
                    "cache.middleware.store_failed",
                    path=request.url.path,
                    error=str(exc),
                )
                # Return original response (best effort) by rebuilding it
                # Since we may have consumed body_iterator, return from body_bytes
                return Response(
                    content=body_bytes,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )

        return response
