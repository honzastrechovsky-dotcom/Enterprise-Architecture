"""Plugin management API endpoints.

All endpoints require admin role. Plugins can be enabled/disabled per tenant
and configured with tenant-specific settings.

GET  /api/v1/plugins                  - List available plugins
POST /api/v1/plugins/{name}/enable    - Enable plugin for tenant
POST /api/v1/plugins/{name}/disable   - Disable plugin for tenant
GET  /api/v1/plugins/{name}           - Get plugin details
GET  /api/v1/plugins/{name}/config    - Get plugin configuration
PUT  /api/v1/plugins/{name}/config    - Update plugin configuration
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.audit import AuditService
from src.core.policy import Permission, apply_tenant_filter, check_permission
from src.database import get_db_session
from src.models.audit import AuditStatus
from src.models.plugin import PluginRegistration
from src.plugins.registry import get_registry

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/plugins", tags=["plugins"])


# ------------------------------------------------------------------ #
# Request/Response models
# ------------------------------------------------------------------ #


class PluginMetadataResponse(BaseModel):
    """Plugin metadata from registry."""

    name: str
    version: str
    author: str
    description: str
    required_permissions: list[str]
    compatible_versions: list[str]


class PluginRegistrationResponse(BaseModel):
    """Plugin registration status for tenant."""

    id: uuid.UUID
    plugin_name: str
    plugin_version: str
    enabled: bool
    config: dict[str, Any]
    installed_at: datetime
    updated_at: datetime


class PluginListResponse(BaseModel):
    """Combined plugin info: available plugins + tenant registrations."""

    available: list[PluginMetadataResponse]
    registered: list[PluginRegistrationResponse]


class PluginConfigUpdate(BaseModel):
    """Plugin configuration update request."""

    config: dict[str, Any] = Field(..., description="Plugin configuration settings")


# ------------------------------------------------------------------ #
# Plugin listing
# ------------------------------------------------------------------ #


@router.get(
    "",
    response_model=PluginListResponse,
    summary="List available plugins and registrations",
)
async def list_plugins(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PluginListResponse:
    """List all available plugins and current tenant's registrations.

    Returns both the catalog of available plugins from the registry and
    which plugins are currently enabled for this tenant.
    """
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    # Get available plugins from registry
    registry = get_registry()
    plugin_metadata_list = registry.list_plugins(tenant_id=str(current_user.tenant_id))

    available = [
        PluginMetadataResponse(
            name=pm.name,
            version=pm.version,
            author=pm.author,
            description=pm.description,
            required_permissions=pm.required_permissions,
            compatible_versions=pm.compatible_versions,
        )
        for pm in plugin_metadata_list
    ]

    # Get tenant's plugin registrations
    stmt = apply_tenant_filter(
        select(PluginRegistration).order_by(PluginRegistration.installed_at.desc()),
        PluginRegistration,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    registrations = result.scalars().all()

    registered = [
        PluginRegistrationResponse(
            id=reg.id,
            plugin_name=reg.plugin_name,
            plugin_version=reg.plugin_version,
            enabled=reg.enabled,
            config=reg.config,
            installed_at=reg.installed_at,
            updated_at=reg.updated_at,
        )
        for reg in registrations
    ]

    return PluginListResponse(
        available=available,
        registered=registered,
    )


# ------------------------------------------------------------------ #
# Plugin details
# ------------------------------------------------------------------ #


@router.get(
    "/{plugin_name}",
    response_model=PluginMetadataResponse,
    summary="Get plugin details",
)
async def get_plugin_details(
    plugin_name: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> PluginMetadataResponse:
    """Get details about a specific plugin."""
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    registry = get_registry()
    plugin = registry.get_plugin(plugin_name, tenant_id=str(current_user.tenant_id))

    if plugin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin_name}' not found",
        )

    metadata = plugin.metadata
    return PluginMetadataResponse(
        name=metadata.name,
        version=metadata.version,
        author=metadata.author,
        description=metadata.description,
        required_permissions=metadata.required_permissions,
        compatible_versions=metadata.compatible_versions,
    )


# ------------------------------------------------------------------ #
# Plugin enable/disable
# ------------------------------------------------------------------ #


@router.post(
    "/{plugin_name}/enable",
    response_model=PluginRegistrationResponse,
    status_code=status.HTTP_200_OK,
    summary="Enable plugin for tenant",
)
async def enable_plugin(
    plugin_name: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PluginRegistrationResponse:
    """Enable a plugin for the current tenant.

    Creates a registration if it doesn't exist, or updates existing to enabled=True.
    """
    check_permission(current_user.role, Permission.ADMIN_USER_WRITE)

    # Verify plugin exists in registry
    registry = get_registry()
    plugin = registry.get_plugin(plugin_name, tenant_id=str(current_user.tenant_id))

    if plugin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin_name}' not found in registry",
        )

    # Check if registration exists
    stmt = apply_tenant_filter(
        select(PluginRegistration).where(PluginRegistration.plugin_name == plugin_name),
        PluginRegistration,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    registration = result.scalar_one_or_none()

    if registration is None:
        # Create new registration
        registration = PluginRegistration(
            tenant_id=current_user.tenant_id,
            plugin_name=plugin_name,
            plugin_version=plugin.metadata.version,
            enabled=True,
            config={},
        )
        db.add(registration)
    else:
        # Update existing to enabled
        registration.enabled = True
        registration.plugin_version = plugin.metadata.version

    await db.flush()

    # Audit log
    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="plugin.enable",
        resource_type="plugin",
        resource_id=str(registration.id),
        status=AuditStatus.SUCCESS,
        extra={"plugin_name": plugin_name, "version": plugin.metadata.version},
    )

    log.info(
        "plugin.enabled",
        plugin_name=plugin_name,
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
    )

    return PluginRegistrationResponse(
        id=registration.id,
        plugin_name=registration.plugin_name,
        plugin_version=registration.plugin_version,
        enabled=registration.enabled,
        config=registration.config,
        installed_at=registration.installed_at,
        updated_at=registration.updated_at,
    )


@router.post(
    "/{plugin_name}/disable",
    response_model=PluginRegistrationResponse,
    status_code=status.HTTP_200_OK,
    summary="Disable plugin for tenant",
)
async def disable_plugin(
    plugin_name: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PluginRegistrationResponse:
    """Disable a plugin for the current tenant.

    Sets enabled=False but keeps the registration and configuration.
    """
    check_permission(current_user.role, Permission.ADMIN_USER_WRITE)

    # Find registration
    stmt = apply_tenant_filter(
        select(PluginRegistration).where(PluginRegistration.plugin_name == plugin_name),
        PluginRegistration,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    registration = result.scalar_one_or_none()

    if registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin_name}' is not registered for this tenant",
        )

    registration.enabled = False
    await db.flush()

    # Audit log
    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="plugin.disable",
        resource_type="plugin",
        resource_id=str(registration.id),
        status=AuditStatus.SUCCESS,
        extra={"plugin_name": plugin_name},
    )

    log.info(
        "plugin.disabled",
        plugin_name=plugin_name,
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
    )

    return PluginRegistrationResponse(
        id=registration.id,
        plugin_name=registration.plugin_name,
        plugin_version=registration.plugin_version,
        enabled=registration.enabled,
        config=registration.config,
        installed_at=registration.installed_at,
        updated_at=registration.updated_at,
    )


# ------------------------------------------------------------------ #
# Plugin configuration
# ------------------------------------------------------------------ #


@router.get(
    "/{plugin_name}/config",
    response_model=dict[str, Any],
    summary="Get plugin configuration",
)
async def get_plugin_config(
    plugin_name: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get current configuration for a plugin."""
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    stmt = apply_tenant_filter(
        select(PluginRegistration).where(PluginRegistration.plugin_name == plugin_name),
        PluginRegistration,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    registration = result.scalar_one_or_none()

    if registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin_name}' is not registered for this tenant",
        )

    return registration.config


@router.put(
    "/{plugin_name}/config",
    response_model=PluginRegistrationResponse,
    summary="Update plugin configuration",
)
async def update_plugin_config(
    plugin_name: str,
    body: PluginConfigUpdate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PluginRegistrationResponse:
    """Update configuration for a plugin."""
    check_permission(current_user.role, Permission.ADMIN_USER_WRITE)

    stmt = apply_tenant_filter(
        select(PluginRegistration).where(PluginRegistration.plugin_name == plugin_name),
        PluginRegistration,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    registration = result.scalar_one_or_none()

    if registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin_name}' is not registered for this tenant",
        )

    registration.config = body.config
    await db.flush()

    # Audit log
    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="plugin.config_update",
        resource_type="plugin",
        resource_id=str(registration.id),
        status=AuditStatus.SUCCESS,
        extra={"plugin_name": plugin_name},
    )

    log.info(
        "plugin.config_updated",
        plugin_name=plugin_name,
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
    )

    return PluginRegistrationResponse(
        id=registration.id,
        plugin_name=registration.plugin_name,
        plugin_version=registration.plugin_version,
        enabled=registration.enabled,
        config=registration.config,
        installed_at=registration.installed_at,
        updated_at=registration.updated_at,
    )
