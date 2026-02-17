"""TenantSettings model - per-tenant configuration and quota overrides.

Each tenant may have a single settings record that overrides platform defaults.
If no record exists the platform defaults (from Settings) apply.

Design:
- One-to-one with Tenant (enforced by UNIQUE constraint on tenant_id)
- JSONB columns for flexible configuration (model config, features, branding)
- All fields are nullable so partial updates are easy
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class TenantSettings(Base):
    __tablename__ = "tenant_settings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ----- Rate limiting -----
    # If NULL the global rate_limit_per_minute from Settings is used
    custom_rate_limit: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Requests per minute override; NULL = use platform default",
    )

    # ----- Model routing -----
    # JSONB blob, e.g. {"default_model": "openai/gpt-4o", "allow_models": ["gpt-4o-mini"]}
    custom_model_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Model routing overrides for this tenant",
    )

    # ----- Feature flags -----
    # Array of feature strings, e.g. ["rag", "plugins", "fine_tuning"]
    enabled_features: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Opt-in feature flags for this tenant",
    )

    # ----- Quotas -----
    max_users: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Maximum number of users; NULL = unlimited",
    )
    max_storage_gb: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Storage quota in GiB; NULL = unlimited",
    )
    token_budget_daily: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Daily token budget override; NULL = use platform default",
    )
    token_budget_monthly: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Monthly token budget override; NULL = use platform default",
    )

    # ----- Customisation -----
    custom_system_prompt: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="System-level prompt prepended to every conversation for this tenant",
    )
    # e.g. {"logo_url": "...", "primary_color": "#123456", "name": "Acme Corp"}
    branding: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Branding overrides for this tenant's UI",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant")  # type: ignore[name-defined]

    __table_args__ = (
        # One settings record per tenant
        UniqueConstraint("tenant_id", name="uq_tenant_settings_tenant_id"),
    )

    def __repr__(self) -> str:
        return f"<TenantSettings tenant_id={self.tenant_id}>"
