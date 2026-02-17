"""Write operation persistence model.

Stores HITL write operations in PostgreSQL so they survive restarts and
support multi-instance deployments.  Each row represents one operation
from proposal through execution.

All reads MUST use apply_tenant_filter() — tenant_id is the isolation boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class WriteOperationRecord(Base):
    """Persisted write operation with full HITL lifecycle.

    Lifecycle:
        proposed -> pending_approval -> approved  -> executing -> completed
                                     -> rejected
                                                  -> failed

        LOW-risk operations skip pending_approval and go directly to approved
        via auto-approval at proposal time.

    Parameters column stores the operation parameters (formerly ``params``
    in the in-memory dataclass).  Result is stored in result_json once
    the operation completes or fails.

    audit_trail stores the ordered list of audit events as a JSON array::

        [
            {"timestamp": "...", "event": "proposed", "actor": "uuid", ...},
            {"timestamp": "...", "event": "approved", "actor": "uuid", ...},
            ...
        ]
    """

    __tablename__ = "write_operations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Write operation primary key",
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="Owning tenant — all queries must filter on this column",
    )

    # Who proposed the operation
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="User who proposed the operation",
    )

    # Connector and operation identity
    connector: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Connector name (sap, mes, etc.)",
    )

    operation_type: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Specific operation within the connector (e.g. create_purchase_request)",
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Human-readable description of the operation",
    )

    # Operation parameters as JSON
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Operation parameters passed to the connector",
    )

    # Risk and approval flags
    risk_level: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="low | medium | high | critical",
    )

    requires_approval: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether operator approval is required",
    )

    requires_mfa: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether MFA verification is required for approval",
    )

    # Lifecycle status
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="proposed",
        server_default="proposed",
        comment=(
            "proposed | pending_approval | approved | rejected"
            " | executing | completed | failed"
        ),
    )

    # Timestamps
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="UTC timestamp when the operation was proposed",
    )

    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp when the operation was approved or auto-approved",
    )

    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp when execution started",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="UTC timestamp of row creation (same as proposed_at in practice)",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        comment="UTC timestamp of last modification",
    )

    # Who approved or rejected
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="User who approved or rejected the operation (null until reviewed)",
    )

    # Execution result
    result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="ConnectorResult serialised as JSON; null until execution completes",
    )

    # Audit trail — ordered list of audit events
    audit_trail: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="Ordered list of audit events from proposal to completion",
    )

    __table_args__ = (
        # Primary list query: all operations for a tenant ordered by recency
        Index("ix_write_operations_tenant_created", "tenant_id", "created_at"),
        # Pending-approval queue: list operations awaiting review for a tenant
        Index("ix_write_operations_tenant_status", "tenant_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<WriteOperationRecord id={self.id} "
            f"tenant={self.tenant_id} "
            f"connector={self.connector!r} "
            f"status={self.status!r}>"
        )
