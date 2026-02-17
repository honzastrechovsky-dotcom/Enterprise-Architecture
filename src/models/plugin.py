"""Plugin registration model.

Tracks which plugins are enabled for each tenant, along with their
configuration. This allows tenant-scoped plugin management.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class PluginRegistration(Base):
    """Records which plugins are enabled for a tenant.

    Each tenant can enable/disable plugins and configure them independently.
    The config field stores plugin-specific settings as JSONB.
    """

    __tablename__ = "plugin_registrations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Plugin identification
    plugin_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Unique plugin identifier",
    )
    plugin_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Plugin version (semver)",
    )

    # Status
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether plugin is currently active for this tenant",
    )

    # Plugin-specific configuration
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Plugin configuration settings (JSON)",
    )

    # Timestamps
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="When plugin was first installed for this tenant",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        comment="Last configuration or status update",
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant")  # type: ignore[name-defined]

    __table_args__ = (
        # One registration per plugin per tenant
        Index(
            "uq_plugin_tenant",
            "tenant_id",
            "plugin_name",
            unique=True,
        ),
        Index("ix_plugin_enabled", "tenant_id", "enabled"),
    )

    def __repr__(self) -> str:
        return (
            f"<PluginRegistration id={self.id} "
            f"plugin={self.plugin_name!r} "
            f"tenant={self.tenant_id} "
            f"enabled={self.enabled}>"
        )
