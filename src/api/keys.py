"""API Key management endpoints.

Provides CRUD operations for API keys within a tenant.
All endpoints require admin role.

POST /keys              - Create a new API key (returns raw key ONCE)
GET  /keys              - List all keys for tenant (no raw keys)
GET  /keys/{id}         - Get key details (no raw key)
DELETE /keys/{id}       - Revoke a key
POST /keys/{id}/rotate  - Rotate a key (returns new raw key ONCE)
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.audit import AuditService
from src.core.policy import Permission, check_permission
from src.database import get_db_session
from src.models.audit import AuditStatus
from src.services.api_keys import APIKeyService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/keys", tags=["api-keys"])


# ------------------------------------------------------------------ #
# Request / Response schemas
# ------------------------------------------------------------------ #


class APIKeyCreate(BaseModel):
    """Request body for creating an API key."""

    name: str = Field(..., min_length=1, max_length=255, description="Human-readable name for the key")
    description: str | None = Field(None, max_length=1000, description="Optional description of the key's purpose")
    scopes: list[str] = Field(
        ...,
        min_length=1,
        description="List of allowed scopes (e.g., ['chat', 'documents', 'analytics'])",
    )
    expires_in_days: int | None = Field(
        None,
        ge=1,
        le=3650,
        description="Days until expiration (None = never expires)",
    )
    rate_limit_per_minute: int | None = Field(
        None,
        ge=1,
        le=10000,
        description="Max requests per minute (None = no limit)",
    )


class APIKeyResponse(BaseModel):
    """API key details (never includes raw key or hash)."""

    id: uuid.UUID
    name: str
    description: str | None
    key_prefix: str
    scopes: list[str]
    rate_limit_per_minute: int | None
    created_by: uuid.UUID | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    is_active: bool
    revoked_at: datetime | None


class APIKeyCreateResponse(APIKeyResponse):
    """Response for key creation and rotation - includes the raw key ONCE."""

    key: str = Field(..., description="Raw API key - save this, it won't be shown again!")
    warning: str = Field(
        default="Save this key immediately. It will not be shown again for security reasons.",
        description="Security reminder",
    )


class APIKeyRevokeResponse(BaseModel):
    """Response for key revocation."""

    id: uuid.UUID
    revoked: bool
    revoked_at: datetime


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _build_key_response(api_key) -> APIKeyResponse:
    """Build an APIKeyResponse from an APIKey model."""
    return APIKeyResponse(
        id=api_key.id,
        name=api_key.name,
        description=api_key.description,
        key_prefix=api_key.key_prefix,
        scopes=api_key.scopes,
        rate_limit_per_minute=api_key.rate_limit_per_minute,
        created_by=api_key.created_by,
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
        last_used_at=api_key.last_used_at,
        is_active=api_key.is_active,
        revoked_at=api_key.revoked_at,
    )


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #


@router.post(
    "",
    response_model=APIKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new API key (admin only)",
)
async def create_api_key(
    body: APIKeyCreate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> APIKeyCreateResponse:
    """Create a new API key for the current tenant.

    Returns the raw key ONCE in the response. The key will not be
    retrievable afterwards - store it immediately.

    Requires admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_WRITE)

    service = APIKeyService(db)
    api_key, raw_key = await service.create_key(
        tenant_id=current_user.tenant_id,
        name=body.name,
        scopes=body.scopes,
        created_by=current_user.id,
        description=body.description,
        expires_in_days=body.expires_in_days,
        rate_limit_per_minute=body.rate_limit_per_minute,
    )

    # Audit log
    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="api_key.create",
        resource_type="api_key",
        resource_id=str(api_key.id),
        status=AuditStatus.SUCCESS,
        extra={
            "name": body.name,
            "scopes": body.scopes,
            "has_expiry": body.expires_in_days is not None,
            "has_rate_limit": body.rate_limit_per_minute is not None,
        },
    )

    return APIKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        description=api_key.description,
        key_prefix=api_key.key_prefix,
        scopes=api_key.scopes,
        rate_limit_per_minute=api_key.rate_limit_per_minute,
        created_by=api_key.created_by,
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
        last_used_at=api_key.last_used_at,
        is_active=api_key.is_active,
        revoked_at=api_key.revoked_at,
        key=raw_key,
    )


@router.get(
    "",
    response_model=list[APIKeyResponse],
    summary="List API keys for tenant (admin only)",
)
async def list_api_keys(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[APIKeyResponse]:
    """List all API keys for the current tenant.

    Raw keys and hashes are never included in list responses.

    Requires admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    service = APIKeyService(db)
    keys = await service.list_keys(current_user.tenant_id)

    return [_build_key_response(k) for k in keys]


@router.get(
    "/{key_id}",
    response_model=APIKeyResponse,
    summary="Get API key details (admin only)",
)
async def get_api_key(
    key_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> APIKeyResponse:
    """Get details for a specific API key.

    Raw key and hash are never included.

    Requires admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    service = APIKeyService(db)
    keys = await service.list_keys(current_user.tenant_id)

    # Find the specific key
    api_key = next((k for k in keys if k.id == key_id), None)

    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    return _build_key_response(api_key)


@router.delete(
    "/{key_id}",
    response_model=APIKeyRevokeResponse,
    summary="Revoke an API key (admin only)",
)
async def revoke_api_key(
    key_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> APIKeyRevokeResponse:
    """Revoke an API key immediately.

    The key will no longer be accepted for authentication.

    Requires admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_WRITE)

    service = APIKeyService(db)
    api_key = await service.revoke_key(key_id, current_user.tenant_id)

    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    # Audit log
    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="api_key.revoke",
        resource_type="api_key",
        resource_id=str(key_id),
        status=AuditStatus.SUCCESS,
        extra={"name": api_key.name},
    )

    return APIKeyRevokeResponse(
        id=api_key.id,
        revoked=True,
        revoked_at=api_key.revoked_at,
    )


@router.post(
    "/{key_id}/rotate",
    response_model=APIKeyCreateResponse,
    summary="Rotate an API key (admin only)",
)
async def rotate_api_key(
    key_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> APIKeyCreateResponse:
    """Rotate an API key.

    Creates a new key with the same configuration and revokes the old key.
    Returns the new raw key ONCE - save it immediately.

    Requires admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_WRITE)

    service = APIKeyService(db)
    try:
        new_key, new_raw_key = await service.rotate_key(key_id, current_user.tenant_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        ) from exc

    # Audit log
    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="api_key.rotate",
        resource_type="api_key",
        resource_id=str(new_key.id),
        status=AuditStatus.SUCCESS,
        extra={
            "old_key_id": str(key_id),
            "new_key_id": str(new_key.id),
            "name": new_key.name,
        },
    )

    return APIKeyCreateResponse(
        id=new_key.id,
        name=new_key.name,
        description=new_key.description,
        key_prefix=new_key.key_prefix,
        scopes=new_key.scopes,
        rate_limit_per_minute=new_key.rate_limit_per_minute,
        created_by=new_key.created_by,
        created_at=new_key.created_at,
        expires_at=new_key.expires_at,
        last_used_at=new_key.last_used_at,
        is_active=new_key.is_active,
        revoked_at=new_key.revoked_at,
        key=new_raw_key,
    )
