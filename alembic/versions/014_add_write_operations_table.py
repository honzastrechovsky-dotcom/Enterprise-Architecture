"""Add write_operations table for Phase 9E persistence.

Revision ID: 014
Revises: 013
Create Date: 2026-02-17

Phase 9E: Move write operations from in-memory dict to PostgreSQL persistence.
Replaces the in-memory _operations dict in WriteOperationExecutor with a
proper PostgreSQL table so operations survive restarts and support
multi-instance deployments.

Table: write_operations
  - id               UUID PK
  - tenant_id        UUID  (NOT NULL, indexed, tenant isolation)
  - requested_by     UUID  (NOT NULL — user who proposed the operation)
  - connector        VARCHAR(100)  (NOT NULL — e.g. "sap", "mes")
  - operation_type   VARCHAR(200)  (NOT NULL — e.g. "create_purchase_request")
  - description      TEXT  (NOT NULL — human-readable description)
  - parameters       JSONB (NOT NULL, default {})
  - risk_level       VARCHAR(20)   (NOT NULL — low|medium|high|critical)
  - requires_approval BOOLEAN (NOT NULL, default true)
  - requires_mfa      BOOLEAN (NOT NULL, default false)
  - status           VARCHAR(30)   proposed|pending_approval|approved|rejected|
                                   executing|completed|failed
  - proposed_at      TIMESTAMP WITH TIME ZONE (NOT NULL)
  - approved_at      TIMESTAMP WITH TIME ZONE (nullable)
  - executed_at      TIMESTAMP WITH TIME ZONE (nullable)
  - created_at       TIMESTAMP WITH TIME ZONE (NOT NULL)
  - updated_at       TIMESTAMP WITH TIME ZONE (NOT NULL)
  - approved_by      UUID  (nullable — operator who approved/rejected)
  - result_json      JSONB (nullable — execution result)
  - audit_trail      JSONB (NOT NULL, default [] — ordered list of audit events)

Indexes:
  - ix_write_operations_tenant_created  (tenant_id, created_at)
  - ix_write_operations_tenant_status   (tenant_id, status)

Notes:
  - No FK to tenants.id — operations must outlive tenant soft-deletes (audit
    durability requirement).
  - No FK to users.id — same rationale.
  - status stored as VARCHAR rather than a PostgreSQL enum for schema
    flexibility (avoids enum migration pain when adding new statuses).
  - audit_trail is append-only from the application layer; stored as JSONB
    array for simplicity (avoids a separate audit_events table at this scale).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create write_operations table with indexes."""

    op.create_table(
        "write_operations",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="Write operation primary key",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning tenant — all queries must filter on this column",
        ),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="User who proposed the operation",
        ),
        sa.Column(
            "connector",
            sa.String(100),
            nullable=False,
            comment="Connector name (sap, mes, etc.)",
        ),
        sa.Column(
            "operation_type",
            sa.String(200),
            nullable=False,
            comment="Specific operation within the connector",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            comment="Human-readable description of the operation",
        ),
        sa.Column(
            "parameters",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Operation parameters passed to the connector",
        ),
        sa.Column(
            "risk_level",
            sa.String(20),
            nullable=False,
            comment="low | medium | high | critical",
        ),
        sa.Column(
            "requires_approval",
            sa.Boolean(),
            nullable=False,
            server_default="true",
            comment="Whether operator approval is required",
        ),
        sa.Column(
            "requires_mfa",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment="Whether MFA verification is required for approval",
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="proposed",
            comment=(
                "proposed | pending_approval | approved | rejected"
                " | executing | completed | failed"
            ),
        ),
        sa.Column(
            "proposed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp when the operation was proposed",
        ),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC timestamp when the operation was approved or auto-approved",
        ),
        sa.Column(
            "executed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC timestamp when execution started",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp of row creation",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp of last modification",
        ),
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="User who approved or rejected the operation",
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB,
            nullable=True,
            comment="ConnectorResult serialised as JSON; null until execution completes",
        ),
        sa.Column(
            "audit_trail",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
            comment="Ordered list of audit events from proposal to completion",
        ),
    )

    # Primary list query: all operations for a tenant ordered by recency
    op.create_index(
        "ix_write_operations_tenant_created",
        "write_operations",
        ["tenant_id", "created_at"],
        comment="List write operations for a tenant ordered by creation time",
    )

    # Pending-approval queue and status filtering
    op.create_index(
        "ix_write_operations_tenant_status",
        "write_operations",
        ["tenant_id", "status"],
        comment="Filter write operations by status within a tenant",
    )


def downgrade() -> None:
    """Drop write_operations table and its indexes."""

    op.drop_index("ix_write_operations_tenant_status", table_name="write_operations")
    op.drop_index("ix_write_operations_tenant_created", table_name="write_operations")
    op.drop_table("write_operations")
