"""IdPConfig model - per-tenant Identity Provider configuration.

Supports both OIDC and SAML 2.0 providers.  Each tenant may have multiple
IdP configurations (e.g. one SAML provider for employees, one OIDC provider
for contractors).

JSONB column group_role_mapping stores a mapping of IdP group name to
platform role, e.g.:
    {"engineering": "operator", "platform-admins": "admin"}
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class IdPProviderType(StrEnum):
    OIDC = "oidc"
    SAML = "saml"


class IdPConfig(Base):
    """Identity Provider configuration for a tenant.

    Columns:
        id              - Primary key UUID.
        tenant_id       - FK to tenants.id (cascade delete).
        provider_type   - OIDC or SAML.
        entity_id       - IdP entity identifier (SAML EntityID or OIDC issuer URL).
        sso_url         - Single Sign-On URL (SAML SSO endpoint or OIDC auth endpoint).
        slo_url         - Single Logout URL (SAML only; NULL for OIDC).
        certificate_pem - IdP signing certificate in PEM format.
        metadata_xml    - Raw SAML metadata XML (NULL for OIDC configurations).
        group_role_mapping - JSON mapping of IdP group names to platform roles.
        enabled         - Whether this configuration is active.
        created_at      - Record creation timestamp.
        updated_at      - Record last-updated timestamp.
    """

    __tablename__ = "idp_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        comment="Primary key UUID",
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Owning tenant (FK → tenants.id)",
    )

    provider_type: Mapped[IdPProviderType] = mapped_column(
        Enum(IdPProviderType, name="idp_provider_type"),
        nullable=False,
        comment="Protocol: oidc or saml",
    )

    # For SAML: the EntityID from the IdP metadata.
    # For OIDC: the issuer URL from the discovery document.
    entity_id: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
        comment="IdP entity identifier or OIDC issuer URL",
    )

    # SAML: HTTP-Redirect or HTTP-POST SSO endpoint.
    # OIDC: Authorization endpoint (or discovery URL).
    sso_url: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
        comment="Single Sign-On URL",
    )

    # SAML SLO endpoint; NULL for OIDC.
    slo_url: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
        comment="Single Logout URL (SAML only)",
    )

    # PEM-encoded X.509 certificate used to verify assertion signatures.
    # For OIDC this holds the JWKS URI or is NULL when dynamic discovery is used.
    certificate_pem: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="IdP signing certificate in PEM format",
    )

    # Raw SAML IdP metadata XML.  Stored for reference and future metadata refresh.
    # NULL for OIDC configurations.
    metadata_xml: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Raw SAML metadata XML (SAML only)",
    )

    # JSONB dict: {"<idp-group-name>": "<platform-role>", ...}
    # Platform roles: "admin" | "operator" | "viewer"
    group_role_mapping: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        default=dict,
        comment="Maps IdP group names to platform roles",
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether this IdP configuration is active",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="Record creation timestamp",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        comment="Record last-updated timestamp",
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship(  # type: ignore[name-defined]
        "Tenant",
        back_populates="idp_configs",
        lazy="selectin",
    )

    __table_args__ = (
        # A tenant cannot have two IdP configs with the same entity_id.
        Index(
            "ix_idp_configs_tenant_entity",
            "tenant_id",
            "entity_id",
            unique=True,
        ),
        # Fast lookup of enabled configs for a tenant.
        Index("ix_idp_configs_tenant_enabled", "tenant_id", "enabled"),
    )

    def __repr__(self) -> str:
        return (
            f"<IdPConfig id={self.id} tenant={self.tenant_id} "
            f"type={self.provider_type} entity_id={self.entity_id!r}>"
        )
