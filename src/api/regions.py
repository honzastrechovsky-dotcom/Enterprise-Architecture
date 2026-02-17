"""Multi-region management API.

GET  /api/v1/regions                          - list all regions with health
GET  /api/v1/regions/{region_name}/health     - detailed region health
POST /api/v1/regions/failover                 - trigger manual failover (admin)
GET  /api/v1/regions/tenant/{tenant_id}/residency  - data residency info
PUT  /api/v1/regions/tenant/{tenant_id}/residency  - update data residency rules
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.policy import Permission, check_permission
from src.multiregion.failover import FailoverManager
from src.multiregion.routing import RegionRouter

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/regions", tags=["regions"])

# ------------------------------------------------------------------ #
# Dependency: shared instances
# These are module-level singletons configured at application startup.
# In production, these are injected via app.state.
# ------------------------------------------------------------------ #

# Default instances (replaced at startup via configure_region_services)
_router: RegionRouter = RegionRouter()
_failover_manager: FailoverManager | None = None


def configure_region_services(
    region_router: RegionRouter,
    failover_manager: FailoverManager,
) -> None:
    """Wire up the module-level singletons at application startup."""
    global _router, _failover_manager
    _router = region_router
    _failover_manager = failover_manager


def get_region_router() -> RegionRouter:
    return _router


def get_failover_manager() -> FailoverManager:
    if _failover_manager is None:
        raise RuntimeError("FailoverManager not configured. Call configure_region_services() at startup.")
    return _failover_manager


# ------------------------------------------------------------------ #
# Schemas
# ------------------------------------------------------------------ #


class RegionSummary(BaseModel):
    name: str
    endpoint: str
    is_primary: bool
    status: str
    latency_ms: float | None
    last_health_check: datetime | None


class RegionHealthDetail(BaseModel):
    name: str
    endpoint: str
    is_primary: bool
    status: str
    latency_ms: float | None
    last_health_check: datetime | None
    allowed_tenants: list[str]
    excluded_tenants: list[str]
    accepts_all_tenants: bool


class FailoverRequest(BaseModel):
    failed_region: str = Field(..., description="Region that has failed or should be taken offline")
    target_region: str = Field(..., description="Region to promote as the new primary")
    reason: str = Field(default="Manual failover", description="Reason for initiating failover")


class FailoverResponse(BaseModel):
    id: str
    failed_region: str
    target_region: str
    state: str
    initiated_by: str
    started_at: datetime
    completed_at: datetime | None


class ResidencyInfo(BaseModel):
    tenant_id: str
    pinned_region: str | None
    allowed_regions: list[str]
    data_residency_enforced: bool


class ResidencyUpdate(BaseModel):
    region_name: str = Field(..., description="Region to pin this tenant to")


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #


@router.get(
    "",
    response_model=list[RegionSummary],
    summary="List all regions with current health",
)
async def list_regions(
    current_user: AuthenticatedUser = Depends(get_current_user),
    router: RegionRouter = Depends(get_region_router),
) -> list[RegionSummary]:
    """Return all configured regions with their current health status.

    Triggers a live health probe for each region before returning.
    """
    health_map = await router.get_region_health()

    summaries: list[RegionSummary] = []
    for region in router.list_regions():
        health_data = health_map.get(region.name, {})
        summaries.append(
            RegionSummary(
                name=region.name,
                endpoint=region.endpoint,
                is_primary=region.is_primary,
                status=health_data.get("status", region.status.value),
                latency_ms=health_data.get("latency_ms"),
                last_health_check=(
                    region.last_health_check
                    if region.last_health_check
                    else None
                ),
            )
        )

    log.info(
        "api.regions.list",
        user_id=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
        count=len(summaries),
    )
    return summaries


@router.get(
    "/{region_name}/health",
    response_model=RegionHealthDetail,
    summary="Detailed health for a specific region",
)
async def get_region_health(
    region_name: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    router: RegionRouter = Depends(get_region_router),
) -> RegionHealthDetail:
    """Return detailed health information for a specific region."""
    region = router.get_region(region_name)
    if not region:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Region '{region_name}' not found",
        )

    # Run a fresh probe for this specific region
    health_map = await router.get_region_health()
    health_data = health_map.get(region_name, {})

    return RegionHealthDetail(
        name=region.name,
        endpoint=region.endpoint,
        is_primary=region.is_primary,
        status=health_data.get("status", region.status.value),
        latency_ms=health_data.get("latency_ms"),
        last_health_check=region.last_health_check,
        allowed_tenants=region.allowed_tenants,
        excluded_tenants=region.excluded_tenants,
        accepts_all_tenants=region.accepts_all_tenants,
    )


@router.post(
    "/failover",
    response_model=FailoverResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger manual failover to a target region (admin only)",
)
async def trigger_failover(
    request: FailoverRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    router: RegionRouter = Depends(get_region_router),
    failover_manager: FailoverManager = Depends(get_failover_manager),
) -> FailoverResponse:
    """Trigger a manual failover.

    Requires admin role. The failed_region will be marked unavailable
    and the target_region will be promoted to primary.
    """
    check_permission(current_user, Permission.ADMIN_TENANT_WRITE)

    # Validate both regions exist
    if not router.get_region(request.failed_region):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Failed region '{request.failed_region}' not found",
        )
    if not router.get_region(request.target_region):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target region '{request.target_region}' not found",
        )

    log.warning(
        "api.regions.failover.triggered",
        user_id=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
        failed_region=request.failed_region,
        target_region=request.target_region,
        reason=request.reason,
    )

    try:
        record = await failover_manager.trigger_failover(
            failed_region=request.failed_region,
            target_region=request.target_region,
            initiated_by=f"manual:{current_user.external_id}",
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    return FailoverResponse(
        id=record.id,
        failed_region=record.failed_region,
        target_region=record.target_region,
        state=record.state.value,
        initiated_by=record.initiated_by,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


@router.get(
    "/tenant/{tenant_id}/residency",
    response_model=ResidencyInfo,
    summary="Get data residency rules for a tenant",
)
async def get_tenant_residency(
    tenant_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    router: RegionRouter = Depends(get_region_router),
) -> ResidencyInfo:
    """Return data residency configuration for the given tenant.

    Non-admin users may only query their own tenant.
    """
    # Non-admins can only see their own residency
    try:
        requested_uuid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid tenant_id format",
        )

    if (
        current_user.role not in ("admin",)
        and current_user.tenant_id != requested_uuid
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    # Build list of regions that allow this tenant
    allowed_regions = [
        r.name
        for r in router.list_regions()
        if r.allows_tenant(tenant_id)
    ]

    # Check for pinned residency
    pinned = router._tenant_residency.get(tenant_id)

    return ResidencyInfo(
        tenant_id=tenant_id,
        pinned_region=pinned,
        allowed_regions=allowed_regions,
        data_residency_enforced=bool(pinned or any(
            not r.accepts_all_tenants for r in router.list_regions()
        )),
    )


@router.put(
    "/tenant/{tenant_id}/residency",
    response_model=ResidencyInfo,
    summary="Update data residency rules for a tenant (admin only)",
)
async def update_tenant_residency(
    tenant_id: str,
    update: ResidencyUpdate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    router: RegionRouter = Depends(get_region_router),
) -> ResidencyInfo:
    """Pin a tenant to a specific region for data residency compliance.

    Requires admin role. Validates that the target region allows this tenant.
    """
    check_permission(current_user, Permission.ADMIN_TENANT_WRITE)

    region = router.get_region(update.region_name)
    if not region:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Region '{update.region_name}' not found",
        )

    if not region.allows_tenant(tenant_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Region '{update.region_name}' does not permit tenant '{tenant_id}'. "
                "Update the region's allowed_tenants configuration first."
            ),
        )

    try:
        router.set_tenant_residency(tenant_id, update.region_name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    log.info(
        "api.regions.residency.updated",
        user_id=str(current_user.id),
        tenant_id=tenant_id,
        pinned_region=update.region_name,
    )

    allowed_regions = [
        r.name
        for r in router.list_regions()
        if r.allows_tenant(tenant_id)
    ]

    return ResidencyInfo(
        tenant_id=tenant_id,
        pinned_region=update.region_name,
        allowed_regions=allowed_regions,
        data_residency_enforced=True,
    )
