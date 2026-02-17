"""Admin endpoints - tenant and user management.

All admin endpoints require the 'admin' role.

POST /admin/tenants              - Create a tenant
GET  /admin/tenants              - List tenants
GET  /admin/tenants/{id}         - Get tenant details
POST /admin/users                - Create/invite a user
GET  /admin/users                - List users in current tenant
PATCH /admin/users/{id}/role     - Change user role
DELETE /admin/users/{id}         - Deactivate user
GET  /admin/audit                - Query audit logs
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.api_key_auth import require_scope
from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.audit import AuditService
from src.core.policy import Permission, apply_tenant_filter, check_permission
from src.database import get_db_session
from src.models.audit import AuditLog, AuditStatus
from src.models.tenant import Tenant
from src.models.user import User, UserRole

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_scope("admin"))])


# ------------------------------------------------------------------ #
# Tenant management
# ------------------------------------------------------------------ #


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    description: str | None = None


class TenantResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    is_active: bool
    created_at: datetime


@router.post(
    "/tenants",
    response_model=TenantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new tenant (super-admin only)",
)
async def create_tenant(
    body: TenantCreate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TenantResponse:
    """Create a new tenant. Only admin users can create tenants."""
    check_permission(current_user.role, Permission.ADMIN_TENANT_WRITE)

    # Check slug uniqueness
    existing = await db.execute(select(Tenant).where(Tenant.slug == body.slug))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant slug {body.slug!r} is already taken",
        )

    tenant = Tenant(
        name=body.name,
        slug=body.slug,
        description=body.description,
    )
    db.add(tenant)
    await db.flush()

    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="admin.tenant.create",
        resource_type="tenant",
        resource_id=str(tenant.id),
        status=AuditStatus.SUCCESS,
        extra={"slug": body.slug},
    )

    return TenantResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        description=tenant.description,
        is_active=tenant.is_active,
        created_at=tenant.created_at,
    )


@router.get(
    "/tenants",
    response_model=list[TenantResponse],
    summary="List all tenants (admin only)",
)
async def list_tenants(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[TenantResponse]:
    """List all tenants."""
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    result = await db.execute(select(Tenant).where(Tenant.deleted_at.is_(None)))
    tenants = result.scalars().all()
    return [
        TenantResponse(
            id=t.id,
            name=t.name,
            slug=t.slug,
            description=t.description,
            is_active=t.is_active,
            created_at=t.created_at,
        )
        for t in tenants
    ]


# ------------------------------------------------------------------ #
# User management
# ------------------------------------------------------------------ #


class UserCreate(BaseModel):
    email: str = Field(..., description="User email address")
    display_name: str | None = None
    external_id: str = Field(..., description="Identity provider subject claim")
    role: UserRole = UserRole.VIEWER


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class UserRoleUpdate(BaseModel):
    role: UserRole


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user in the current tenant",
)
async def create_user(
    body: UserCreate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """Pre-provision a user in the current tenant."""
    check_permission(current_user.role, Permission.ADMIN_USER_WRITE)

    # Check uniqueness within tenant
    existing = await db.execute(
        select(User).where(
            User.tenant_id == current_user.tenant_id,
            User.external_id == body.external_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this external_id already exists in this tenant",
        )

    user = User(
        tenant_id=current_user.tenant_id,
        external_id=body.external_id,
        email=body.email,
        display_name=body.display_name,
        role=body.role,
    )
    db.add(user)
    await db.flush()

    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="admin.user.create",
        resource_type="user",
        resource_id=str(user.id),
        status=AuditStatus.SUCCESS,
    )

    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.get(
    "/users",
    response_model=list[UserResponse],
    summary="List users in the current tenant",
)
async def list_users(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[UserResponse]:
    """List all users in the current tenant."""
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    stmt = apply_tenant_filter(
        select(User).order_by(User.created_at.desc()),
        User,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    users = result.scalars().all()

    return [
        UserResponse(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role.value,
            is_active=u.is_active,
            created_at=u.created_at,
            last_login_at=u.last_login_at,
        )
        for u in users
    ]


@router.patch(
    "/users/{user_id}/role",
    response_model=UserResponse,
    summary="Update a user's role",
)
async def update_user_role(
    user_id: uuid.UUID,
    body: UserRoleUpdate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """Change a user's role within the current tenant."""
    check_permission(current_user.role, Permission.ADMIN_USER_WRITE)

    stmt = apply_tenant_filter(
        select(User).where(User.id == user_id),
        User,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    old_role = user.role
    user.role = body.role

    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="admin.user.role_change",
        resource_type="user",
        resource_id=str(user_id),
        status=AuditStatus.SUCCESS,
        extra={"old_role": old_role.value, "new_role": body.role.value},
    )

    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


# ------------------------------------------------------------------ #
# Audit log query
# ------------------------------------------------------------------ #


class AuditLogResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    timestamp: datetime
    action: str
    resource_type: str | None
    resource_id: str | None
    model_used: str | None
    status: str
    latency_ms: int | None
    request_summary: str | None
    response_summary: str | None


@router.get(
    "/audit",
    response_model=list[AuditLogResponse],
    summary="Query audit logs (admin only)",
)
async def query_audit_logs(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    action: str | None = Query(default=None, description="Filter by action"),
    user_id: uuid.UUID | None = Query(default=None, description="Filter by user"),
    limit: int = Query(default=50, le=500),
    offset: int = 0,
) -> list[AuditLogResponse]:
    """Query audit logs for the current tenant."""
    check_permission(current_user.role, Permission.AUDIT_READ)

    stmt = apply_tenant_filter(
        select(AuditLog).order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit),
        AuditLog,
        current_user.tenant_id,
    )
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)

    result = await db.execute(stmt)
    logs = result.scalars().all()

    return [
        AuditLogResponse(
            id=entry.id,
            user_id=entry.user_id,
            timestamp=entry.timestamp,
            action=entry.action,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            model_used=entry.model_used,
            status=entry.status,
            latency_ms=entry.latency_ms,
            request_summary=entry.request_summary,
            response_summary=entry.response_summary,
        )
        for entry in logs
    ]
