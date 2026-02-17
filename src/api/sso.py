"""SSO / SAML 2.0 endpoints.

Prefix: /api/v1/sso

Routes:
    GET  /saml/metadata/{tenant_id}          - Return SP metadata XML
    POST /saml/acs                           - Assertion Consumer Service
    GET  /saml/login/{tenant_id}             - Redirect to IdP
    GET  /providers/{tenant_id}              - List configured IdPs for a tenant
    POST /providers/{tenant_id}              - Add a new IdP configuration
    DELETE /providers/{tenant_id}/{provider_id} - Remove an IdP configuration

Authentication:
    - The ACS and metadata endpoints are public (no auth required) because
      they are part of the SAML flow itself.
    - The /providers/* management endpoints require ADMIN role.
    - The /saml/login redirect is public.

Design notes:
    - On successful ACS callback the endpoint issues a platform JWT so that
      subsequent API calls use the standard Bearer-token auth path.
    - IdP configurations are stored in idp_configs table (IdPConfig model).
    - One tenant may have N IdP configurations (multi-IdP support).
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, require_role
from src.auth.saml import IdPConfiguration, SAMLAuthProvider, SAMLValidationError
from src.config import Settings, get_settings
from src.database import get_db_session
from src.models.idp_config import IdPConfig, IdPProviderType
from src.models.user import UserRole

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/sso", tags=["sso"])

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class IdPConfigCreate(BaseModel):
    """Request body for creating a new IdP configuration."""

    provider_type: IdPProviderType = Field(
        ..., description="Protocol: 'oidc' or 'saml'"
    )
    entity_id: str = Field(..., description="IdP entity ID or OIDC issuer URL")
    sso_url: str = Field(..., description="SSO endpoint URL")
    slo_url: str | None = Field(None, description="SLO endpoint URL (SAML only)")
    certificate_pem: str | None = Field(
        None, description="PEM-encoded IdP signing certificate"
    )
    metadata_xml: str | None = Field(
        None, description="Raw SAML metadata XML (optional)"
    )
    group_role_mapping: dict[str, str] = Field(
        default_factory=dict,
        description='Map IdP group -> platform role, e.g. {"admins": "admin"}',
    )
    enabled: bool = Field(True, description="Whether to enable this configuration")


class IdPConfigResponse(BaseModel):
    """Serialised IdP configuration returned by the API."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    provider_type: str
    entity_id: str
    sso_url: str
    slo_url: str | None
    group_role_mapping: dict[str, str]
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IdPConfigListResponse(BaseModel):
    providers: list[IdPConfigResponse]
    total: int


class ACSResponse(BaseModel):
    """Result of a successful SAML ACS callback."""

    access_token: str
    token_type: str = "bearer"
    user_id: str
    tenant_id: str
    email: str | None
    role: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _orm_to_runtime_config(orm: IdPConfig) -> IdPConfiguration:
    """Convert an ORM IdPConfig row to the runtime IdPConfiguration dataclass."""
    return IdPConfiguration(
        idp_id=orm.id,
        tenant_id=orm.tenant_id,
        entity_id=orm.entity_id,
        sso_url=orm.sso_url,
        slo_url=orm.slo_url,
        certificate_pem=orm.certificate_pem or "",
        group_role_mapping=dict(orm.group_role_mapping or {}),
    )


async def _get_idp_config_or_404(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider_id: uuid.UUID,
) -> IdPConfig:
    stmt = select(IdPConfig).where(
        IdPConfig.tenant_id == tenant_id,
        IdPConfig.id == provider_id,
    )
    result = await db.execute(stmt)
    orm = result.scalar_one_or_none()
    if orm is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"IdP configuration {provider_id} not found for tenant {tenant_id}.",
        )
    return orm


async def _get_enabled_idp_for_tenant(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider_type: IdPProviderType = IdPProviderType.SAML,
) -> IdPConfig:
    """Return the first enabled IdP config for a tenant, or raise 404."""
    stmt = (
        select(IdPConfig)
        .where(
            IdPConfig.tenant_id == tenant_id,
            IdPConfig.provider_type == provider_type,
            IdPConfig.enabled.is_(True),
        )
        .order_by(IdPConfig.created_at)
        .limit(1)
    )
    result = await db.execute(stmt)
    orm = result.scalar_one_or_none()
    if orm is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No enabled {provider_type.upper()} IdP configured for tenant {tenant_id}.",
        )
    return orm


def _issue_platform_jwt(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    email: str,
    role: str,
    settings: Settings,
) -> str:
    """Issue a platform JWT after successful SAML authentication.

    In production this should use an asymmetric key.  We reuse the dev JWT
    mechanism here; a production deployment should swap this for RS256.
    """
    from src.auth.oidc import create_dev_token

    return create_dev_token(
        sub=str(user_id),
        tenant_id=str(tenant_id),
        role=role,
        email=email,
        secret=settings.dev_jwt_secret.get_secret_value(),
        expires_in=3600,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/saml/metadata/{tenant_id}",
    response_class=Response,
    summary="Return SP metadata XML for a tenant",
    tags=["sso"],
)
async def get_saml_metadata(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Return the Service Provider metadata XML for this platform.

    The XML should be provided to IdP administrators during federation setup.
    """
    # Verify tenant has at least one SAML config so we don't expose metadata
    # for unconfigured tenants.
    await _get_enabled_idp_for_tenant(db, tenant_id, IdPProviderType.SAML)

    acs_url = f"{settings.public_base_url.rstrip('/')}/api/v1/sso/saml/acs"
    provider = SAMLAuthProvider(sp_entity_id=settings.public_base_url.rstrip("/"))
    xml = provider.build_sp_metadata(acs_url)

    return Response(
        content=xml,
        media_type="application/samlmetadata+xml",
    )


@router.get(
    "/saml/login/{tenant_id}",
    summary="Initiate SAML login - redirect to IdP",
    tags=["sso"],
)
async def saml_login(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Redirect the browser to the IdP SSO endpoint to begin SAML authentication."""
    orm = await _get_enabled_idp_for_tenant(db, tenant_id, IdPProviderType.SAML)
    idp_config = _orm_to_runtime_config(orm)

    provider = SAMLAuthProvider(sp_entity_id=settings.public_base_url.rstrip("/"))
    auth_request = await provider.build_auth_request(idp_config)

    log.info(
        "sso.saml_login_redirect",
        tenant_id=str(tenant_id),
        idp_entity_id=idp_config.entity_id,
        request_id=auth_request["request_id"],
    )

    return RedirectResponse(url=auth_request["redirect_url"], status_code=302)


@router.post(
    "/saml/acs",
    response_model=ACSResponse,
    summary="SAML Assertion Consumer Service",
    tags=["sso"],
)
async def saml_acs(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ACSResponse:
    """Process a SAML Response from the IdP.

    The IdP POSTs a base64-encoded SAML Response to this endpoint after
    successful user authentication.  This endpoint:
    1. Decodes and validates the SAML Response.
    2. Extracts user identity and group memberships.
    3. Creates or updates the user record (JIT provisioning).
    4. Issues a platform JWT and returns it to the client.
    """
    form = await request.form()
    saml_response_b64 = form.get("SAMLResponse")
    if not saml_response_b64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing SAMLResponse in POST body.",
        )

    try:
        saml_xml = base64.b64decode(str(saml_response_b64))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAMLResponse is not valid base64.",
        )

    # Extract tenant_id from RelayState (we encode it as "tenant:<uuid>")
    relay_state = str(form.get("RelayState", ""))
    tenant_id: uuid.UUID | None = None
    if relay_state.startswith("tenant:"):
        try:
            tenant_id = uuid.UUID(relay_state.removeprefix("tenant:").split(":")[0])
        except ValueError:
            pass

    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot determine tenant from RelayState.",
        )

    orm = await _get_enabled_idp_for_tenant(db, tenant_id, IdPProviderType.SAML)
    idp_config = _orm_to_runtime_config(orm)

    provider = SAMLAuthProvider(sp_entity_id=settings.public_base_url.rstrip("/"))

    try:
        parsed = await provider.parse_saml_response(saml_xml, idp_config)
    except SAMLValidationError as exc:
        log.warning(
            "sso.saml_acs.validation_failed",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"SAML validation failed: {exc}",
        )

    # Map groups to role
    role = await provider.map_groups_to_roles(parsed["groups"], idp_config)
    claims = parsed["claims"]
    email = claims.get("email") or parsed["name_id"]

    # JIT user provisioning
    from sqlalchemy import select as sa_select

    from src.models.user import User

    stmt = sa_select(User).where(
        User.tenant_id == tenant_id,
        User.external_id == parsed["name_id"],
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            tenant_id=tenant_id,
            external_id=parsed["name_id"],
            email=email,
            display_name=claims.get("name"),
            role=role,
        )
        db.add(user)
        await db.flush()
        log.info(
            "sso.saml_acs.user_provisioned",
            user_id=str(user.id),
            tenant_id=str(tenant_id),
            role=role,
        )
    else:
        # Update role from IdP groups on every login
        user.role = role
        user.last_login_at = datetime.now(UTC)

    access_token = _issue_platform_jwt(
        user_id=user.id,
        tenant_id=tenant_id,
        email=email,
        role=role.value,
        settings=settings,
    )

    return ACSResponse(
        access_token=access_token,
        user_id=str(user.id),
        tenant_id=str(tenant_id),
        email=email,
        role=role.value,
    )


@router.get(
    "/providers/{tenant_id}",
    response_model=IdPConfigListResponse,
    summary="List IdP configurations for a tenant",
    tags=["sso"],
)
async def list_providers(
    tenant_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_role(UserRole.ADMIN)),
    db: AsyncSession = Depends(get_db_session),
) -> IdPConfigListResponse:
    """Return all IdP configurations for a tenant.

    Requires ADMIN role.  Only returns configs scoped to the authenticated
    user's tenant (tenant_id in the path must match the JWT).
    """
    if current_user.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to IdP configurations for this tenant.",
        )

    stmt = (
        select(IdPConfig)
        .where(IdPConfig.tenant_id == tenant_id)
        .order_by(IdPConfig.created_at)
    )
    result = await db.execute(stmt)
    configs = result.scalars().all()

    return IdPConfigListResponse(
        providers=[IdPConfigResponse.model_validate(c) for c in configs],
        total=len(configs),
    )


@router.post(
    "/providers/{tenant_id}",
    response_model=IdPConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add an IdP configuration for a tenant",
    tags=["sso"],
)
async def create_provider(
    tenant_id: uuid.UUID,
    body: IdPConfigCreate,
    current_user: AuthenticatedUser = Depends(require_role(UserRole.ADMIN)),
    db: AsyncSession = Depends(get_db_session),
) -> IdPConfigResponse:
    """Add a new IdP configuration (OIDC or SAML) for a tenant.

    Requires ADMIN role.  The entity_id must be unique per tenant.
    """
    if current_user.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to manage IdP configurations for this tenant.",
        )

    # Uniqueness check: entity_id per tenant
    existing_stmt = select(IdPConfig).where(
        IdPConfig.tenant_id == tenant_id,
        IdPConfig.entity_id == body.entity_id,
    )
    existing_result = await db.execute(existing_stmt)
    if existing_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An IdP configuration with entity_id '{body.entity_id}' already exists.",
        )

    orm = IdPConfig(
        tenant_id=tenant_id,
        provider_type=body.provider_type,
        entity_id=body.entity_id,
        sso_url=body.sso_url,
        slo_url=body.slo_url,
        certificate_pem=body.certificate_pem,
        metadata_xml=body.metadata_xml,
        group_role_mapping=body.group_role_mapping,
        enabled=body.enabled,
    )
    db.add(orm)
    await db.flush()

    log.info(
        "sso.provider_created",
        provider_id=str(orm.id),
        tenant_id=str(tenant_id),
        provider_type=body.provider_type,
        entity_id=body.entity_id,
    )

    return IdPConfigResponse.model_validate(orm)


@router.delete(
    "/providers/{tenant_id}/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an IdP configuration",
    tags=["sso"],
)
async def delete_provider(
    tenant_id: uuid.UUID,
    provider_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_role(UserRole.ADMIN)),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete an IdP configuration.

    Requires ADMIN role.  This operation is irreversible; users who relied
    on this IdP will no longer be able to authenticate via SAML/OIDC through
    this configuration.
    """
    if current_user.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to manage IdP configurations for this tenant.",
        )

    orm = await _get_idp_config_or_404(db, tenant_id, provider_id)
    await db.delete(orm)

    log.info(
        "sso.provider_deleted",
        provider_id=str(provider_id),
        tenant_id=str(tenant_id),
    )
