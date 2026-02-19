"""Monitoring models for proactive MES alerting.

Two tables:
- AlertThreshold: per-tenant, per-metric configurable thresholds
- MonitoringAlert: alert history with lifecycle (open -> acknowledged -> resolved)

All records are tenant-scoped. AlertThreshold rows are upserted by tenant
admins; MonitoringAlert rows are created by the ProactiveMonitorService
and updated by operators acknowledging or resolving them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class AlertSeverity(StrEnum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    """Monitoring alert lifecycle states."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class AlertThreshold(Base):
    """Per-tenant alert threshold configuration.

    Each row defines a single metric threshold for a tenant. Multiple
    metrics per tenant are stored as separate rows, allowing fine-grained
    enable/disable control.

    Example metrics: machine_downtime_minutes, defect_rate_percent,
    inventory_below_minimum, quality_deviation_sigma.
    """

    __tablename__ = "alert_thresholds"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="e.g. 'machine_downtime_minutes', 'defect_rate_percent'",
    )
    threshold_value: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Numeric threshold; interpretation depends on metric_name",
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        String(32),
        nullable=False,
        default=AlertSeverity.WARNING,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant")  # type: ignore[name-defined]

    __table_args__ = (
        Index("ix_threshold_tenant_metric", "tenant_id", "metric_name", unique=True),
    )

    def __repr__(self) -> str:
        return (
            f"<AlertThreshold tenant={self.tenant_id} "
            f"metric={self.metric_name!r} value={self.threshold_value}>"
        )


class MonitoringAlert(Base):
    """Monitoring alert record with full lifecycle tracking.

    Created by the ProactiveMonitorService when a metric breaches its
    threshold. Operators can acknowledge alerts via the API. Alerts are
    auto-resolved when the metric returns to normal, or manually resolved.
    """

    __tablename__ = "monitoring_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )
    current_value: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Metric value at the time the alert fired",
    )
    threshold_value: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Threshold that was breached",
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        String(32),
        nullable=False,
    )
    status: Mapped[AlertStatus] = mapped_column(
        String(32),
        nullable=False,
        default=AlertStatus.OPEN,
        index=True,
    )
    message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable alert description",
    )
    recommended_action: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Suggested remediation steps",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    acknowledged_by: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="User email or ID who acknowledged the alert",
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant")  # type: ignore[name-defined]

    __table_args__ = (
        Index("ix_alert_tenant_status", "tenant_id", "status"),
        Index("ix_alert_tenant_created", "tenant_id", "created_at"),
        Index("ix_alert_tenant_metric_status", "tenant_id", "metric_name", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<MonitoringAlert id={self.id} metric={self.metric_name!r} "
            f"status={self.status} severity={self.severity}>"
        )
