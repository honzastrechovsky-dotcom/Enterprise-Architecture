"""Cache management API endpoints.

Admin-only endpoints for inspecting and managing the response cache.

GET  /api/v1/cache/stats      - Cache statistics (admin only)
POST /api/v1/cache/invalidate - Invalidate all cache entries for the current tenant (admin only)
DELETE /api/v1/cache/flush    - Flush all cache entries globally (admin only)

All endpoints require the ADMIN role. The response cache instance is
retrieved via FastAPI dependency injection so it can be replaced in tests.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.cache.backend import CacheBackend, get_cache_backend
from src.cache.response_cache import ResponseCache
from src.config import Settings, get_settings
from src.core.policy import Permission, check_permission

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/cache", tags=["cache"])


# ---------------------------------------------------------------------------
# Dependency: resolve ResponseCache from application settings
# ---------------------------------------------------------------------------


def get_response_cache(settings: Settings = Depends(get_settings)) -> ResponseCache:
    """Build a ResponseCache backed by the configured backend.

    This dependency is injected into each endpoint. In tests it can be
    overridden with app.dependency_overrides.
    """
    backend: CacheBackend = get_cache_backend(settings)
    return ResponseCache(backend=backend)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class CacheStatsResponse(BaseModel):
    backend: str
    connected: bool
    total_keys: int
    hits: int
    misses: int
    hit_rate: float
    used_memory_human: str
    extra: dict[str, Any] = {}


class InvalidateResponse(BaseModel):
    tenant_id: str
    keys_deleted: int
    message: str


class FlushResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/stats",
    response_model=CacheStatsResponse,
    summary="Cache statistics (admin only)",
)
async def get_cache_stats(
    current_user: AuthenticatedUser = Depends(get_current_user),
    cache: ResponseCache = Depends(get_response_cache),
) -> CacheStatsResponse:
    """Return cache hit/miss statistics and backend info.

    Requires ADMIN role. Useful for monitoring cache effectiveness and
    diagnosing whether Redis is reachable.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    stats = await cache.get_cache_stats()

    return CacheStatsResponse(
        backend=stats.get("backend", "unknown"),
        connected=stats.get("connected", False),
        total_keys=int(stats.get("total_keys", 0)),
        hits=int(stats.get("hits", 0)),
        misses=int(stats.get("misses", 0)),
        hit_rate=float(stats.get("hit_rate", 0.0)),
        used_memory_human=str(stats.get("used_memory_human", "n/a")),
        extra=stats.get("extra", {}),
    )


@router.post(
    "/invalidate",
    response_model=InvalidateResponse,
    summary="Invalidate tenant cache (admin only)",
)
async def invalidate_tenant_cache(
    current_user: AuthenticatedUser = Depends(get_current_user),
    cache: ResponseCache = Depends(get_response_cache),
) -> InvalidateResponse:
    """Remove all cached responses for the current tenant.

    Requires ADMIN role. Use this after bulk document ingestion or when
    tenant configuration changes and stale responses must be evicted.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_WRITE)

    tenant_id = current_user.tenant_id
    keys_deleted = await cache.invalidate_tenant(tenant_id)

    log.info(
        "cache.api.tenant_invalidated",
        tenant_id=str(tenant_id),
        keys_deleted=keys_deleted,
        admin_user=str(current_user.id),
    )

    return InvalidateResponse(
        tenant_id=str(tenant_id),
        keys_deleted=keys_deleted,
        message=f"Invalidated {keys_deleted} cache entries for tenant {tenant_id}",
    )


@router.delete(
    "/flush",
    response_model=FlushResponse,
    summary="Flush all cache (admin only)",
)
async def flush_all_cache(
    current_user: AuthenticatedUser = Depends(get_current_user),
    cache: ResponseCache = Depends(get_response_cache),
) -> FlushResponse:
    """Remove ALL entries from the cache regardless of tenant.

    Requires ADMIN role. This is a destructive operation that affects all
    tenants. Use sparingly - prefer per-tenant invalidation in most cases.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_WRITE)

    await cache._backend.flush_all()

    log.warning(
        "cache.api.flushed_all",
        admin_user=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
    )

    return FlushResponse(message="Cache flushed successfully")
