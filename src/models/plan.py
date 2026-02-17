"""Execution plan persistence model.

Stores decomposed goal plans (TaskGraph + metadata) in PostgreSQL.
Each plan belongs to a single tenant; all reads must filter by tenant_id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class PlanRecord(Base):
    """Persisted execution plan.

    Lifecycle:
        draft -> approved -> executing -> complete
                          -> rejected
                          -> failed

    graph_json stores the full TaskGraph as a JSON object:
        {
            "nodes": {
                "<task_id>": {
                    "id": str,
                    "description": str,
                    "agent_id": str,
                    "dependencies": [str, ...],
                    "status": str,
                    "result_content": str | null
                },
                ...
            }
        }
    """

    __tablename__ = "execution_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Plan primary key",
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="Owning tenant — all queries must filter on this column",
    )

    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="User who created the plan",
    )

    goal: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="High-level goal that was decomposed",
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
        server_default="draft",
        comment="draft | approved | rejected | executing | complete | failed",
    )

    graph_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Serialised TaskGraph (nodes dict keyed by task_id)",
    )

    execution_plan: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Human-readable execution order description",
    )

    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Arbitrary metadata (context, comments, etc.)",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="UTC timestamp of plan creation",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        comment="UTC timestamp of last modification",
    )

    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="User who approved the plan (null until approved)",
    )

    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the plan was approved (null until approved)",
    )

    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="User who rejected the plan (null unless rejected)",
    )

    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the plan was rejected (null unless rejected)",
    )

    __table_args__ = (
        # Primary access pattern: all plans for a tenant, newest first
        Index("ix_execution_plans_tenant_created", "tenant_id", "created_at"),
        # Status filter queries (e.g. list pending approvals)
        Index("ix_execution_plans_tenant_status", "tenant_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<PlanRecord id={self.id} "
            f"tenant={self.tenant_id} "
            f"status={self.status!r}>"
        )
