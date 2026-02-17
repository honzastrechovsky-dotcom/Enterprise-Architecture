"""Tenant Administration Portal API.

All endpoints require the caller to hold the 'admin' role within the tenant.
They operate exclusively on the caller's own tenant - there is no super-admin
cross-tenant access via these routes (that lives in /api/v1/admin/*).

Routes:
    GET  /api/v1/tenant             - Tenant details (users, storage, token usage)
    PATCH /api/v1/tenant/settings   - Update tenant settings
    GET  /api/v1/tenant/users       - List users in the tenant
    POST /api/v1/tenant/users/invite - Invite a user
    PATCH /api/v1/tenant/users/{id}/role - Change user role
    POST /api/v1/tenant/users/{id}/deactivate - Deactivate a user
    GET  /api/v1/tenant/usage       - Usage dashboard data
    GET  /api/v1/tenant/quota       - Quota information
    PATCH /api/v1/tenant/quota      - Update quota settings
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.policy import Permission, check_permission
from src.database import get_db_session
from src.models.user import UserRole
from src.services.tenant_admin import (
    QuotaInfo,
    TenantAdminService,
    TenantDetails,
    TenantSettingsResponse,
    TenantSettingsUpdate,
    UsageSummary,
    UserInviteResult,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/tenant", tags=["tenant-admin"])


# ------------------------------------------------------------------ #
# Helper: enforce admin role
# ------------------------------------------------------------------ #


def _require_admin(current_user: AuthenticatedUser) -> None:
    """Raise HTTP 403 if the caller is not a tenant admin."""
    check_permission(current_user.role, Permission.ADMIN_USER_READ)


# ------------------------------------------------------------------ #
# Request / Response schemas (API layer only)
# ------------------------------------------------------------------ #


class UserResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class InviteUserRequest(BaseModel):
    email: str = Field(..., description="Email address to invite")
    role: UserRole = Field(UserRole.VIEWER, description="Role to assign")
    display_name: str | None = Field(None, description="Optional display name")


class UpdateRoleRequest(BaseModel):
    role: UserRole


class UpdateQuotaRequest(BaseModel):
    max_users: int | None = Field(None, ge=1, description="Maximum users; omit to clear limit")
    max_storage_gb: int | None = Field(None, ge=1, description="Storage limit in GiB; omit to clear")
    token_budget_daily: int | None = Field(None, ge=1000, description="Daily token budget override")
    token_budget_monthly: int | None = Field(None, ge=10000, description="Monthly token budget override")


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #


@router.get(
    "",
    response_model=TenantDetails,
    summary="Get current tenant details",
)
async def get_tenant(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TenantDetails:
    """Return enriched details for the caller's tenant.

    Includes user counts, storage estimates, and token usage today/this month.
    Requires admin role.
    """
    _require_admin(current_user)
    service = TenantAdminService(db)
    try:
        return await service.get_tenant_details(current_user.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.patch(
    "/settings",
    response_model=TenantSettingsResponse,
    summary="Update tenant settings",
)
async def update_tenant_settings(
    body: TenantSettingsUpdate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TenantSettingsResponse:
    """Apply a partial settings update for the caller's tenant.

    Supports PATCH semantics - only fields present in the request body
    are updated. Other settings are left unchanged.
    Requires admin role.
    """
    _require_admin(current_user)
    service = TenantAdminService(db)
    return await service.update_tenant_settings(
        tenant_id=current_user.tenant_id,
        update=body,
        actor_user_id=current_user.id,
    )


@router.get(
    "/settings",
    response_model=TenantSettingsResponse,
    summary="Get tenant settings",
)
async def get_tenant_settings(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TenantSettingsResponse:
    """Return current settings for the caller's tenant.

    Returns defaults if no custom settings have been configured.
    Requires admin role.
    """
    _require_admin(current_user)
    service = TenantAdminService(db)
    return await service.get_tenant_settings(current_user.tenant_id)


@router.get(
    "/users",
    response_model=list[UserResponse],
    summary="List users in the tenant",
)
async def list_tenant_users(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[UserResponse]:
    """Return paginated list of users in the caller's tenant.

    Requires admin role.
    """
    _require_admin(current_user)
    service = TenantAdminService(db)
    users = await service.list_tenant_users(
        tenant_id=current_user.tenant_id,
        limit=limit,
        offset=offset,
    )
    return [
        UserResponse(
            id=u.id,
            tenant_id=u.tenant_id,
            email=u.email,
            display_name=u.display_name,
            role=u.role.value,
            is_active=u.is_active,
            created_at=u.created_at,
            last_login_at=u.last_login_at,
        )
        for u in users
    ]


@router.post(
    "/users/invite",
    response_model=UserInviteResult,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a user to the tenant",
)
async def invite_user(
    body: InviteUserRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserInviteResult:
    """Create a pending user invitation.

    The user is created with is_active=False and a synthetic external_id.
    When the user completes sign-up through the identity provider the
    external_id is reconciled. Requires admin role.
    """
    _require_admin(current_user)
    service = TenantAdminService(db)
    try:
        return await service.invite_user(
            tenant_id=current_user.tenant_id,
            email=body.email,
            role=body.role,
            actor_user_id=current_user.id,
            display_name=body.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch(
    "/users/{user_id}/role",
    response_model=UserResponse,
    summary="Change a user's role",
)
async def update_user_role(
    user_id: uuid.UUID,
    body: UpdateRoleRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """Change the role of a user within the caller's tenant.

    Requires admin role. Returns 404 if the target user is not found
    within the tenant (prevents cross-tenant information leakage).
    """
    _require_admin(current_user)
    service = TenantAdminService(db)
    try:
        user = await service.update_user_role(
            tenant_id=current_user.tenant_id,
            user_id=user_id,
            new_role=body.role,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return UserResponse(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.post(
    "/users/{user_id}/deactivate",
    response_model=UserResponse,
    summary="Deactivate a user",
)
async def deactivate_user(
    user_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """Soft-deactivate a user within the caller's tenant.

    Sets is_active=False. The user record is preserved for audit purposes.
    Requires admin role. Admins cannot deactivate their own account.
    """
    _require_admin(current_user)
    service = TenantAdminService(db)
    try:
        user = await service.deactivate_user(
            tenant_id=current_user.tenant_id,
            user_id=user_id,
            actor_user_id=current_user.id,
        )
    except ValueError as exc:
        detail = str(exc)
        if "not found" in detail:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc

    return UserResponse(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.get(
    "/usage",
    response_model=UsageSummary,
    summary="Get tenant usage dashboard data",
)
async def get_tenant_usage(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    date_from: date = Query(
        default=None,
        description="Start date (inclusive). Defaults to 30 days ago.",
    ),
    date_to: date = Query(
        default=None,
        description="End date (inclusive). Defaults to today.",
    ),
) -> UsageSummary:
    """Return aggregated usage metrics for a date range.

    Defaults to the last 30 days. Requires admin role.
    """
    _require_admin(current_user)

    today = datetime.now(UTC).date()
    if date_to is None:
        date_to = today
    if date_from is None:
        from datetime import timedelta
        date_from = today - timedelta(days=29)

    if date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from must be on or before date_to",
        )

    service = TenantAdminService(db)
    return await service.get_tenant_usage(
        tenant_id=current_user.tenant_id,
        date_from=date_from,
        date_to=date_to,
    )


@router.get(
    "/quota",
    response_model=QuotaInfo,
    summary="Get tenant quota information",
)
async def get_tenant_quota(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> QuotaInfo:
    """Return quota limits and current consumption for the caller's tenant.

    Combines configured overrides with platform defaults. Requires admin role.
    """
    _require_admin(current_user)
    service = TenantAdminService(db)
    return await service.get_tenant_quota(current_user.tenant_id)


@router.patch(
    "/quota",
    response_model=QuotaInfo,
    summary="Update tenant quota settings",
)
async def update_tenant_quota(
    body: UpdateQuotaRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> QuotaInfo:
    """Update quota limits for the caller's tenant.

    Pass null/omit a field to clear that override (falls back to platform
    default). Requires admin role.
    """
    _require_admin(current_user)
    service = TenantAdminService(db)
    return await service.update_tenant_quota(
        tenant_id=current_user.tenant_id,
        max_users=body.max_users,
        max_storage_gb=body.max_storage_gb,
        token_budget_daily=body.token_budget_daily,
        token_budget_monthly=body.token_budget_monthly,
        actor_user_id=current_user.id,
    )
