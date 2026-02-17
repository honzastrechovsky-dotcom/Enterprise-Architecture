"""Add execution_plans table for Phase 7B plan persistence.

Revision ID: 013
Revises: 012
Create Date: 2026-02-17

Phase 7B: Persist in-memory stores
Replaces the in-memory _plans_storage dict in src/api/plans.py with a
proper PostgreSQL table.

Table: execution_plans
  - id              UUID PK
  - tenant_id       UUID  (NOT NULL, indexed, tenant isolation)
  - created_by      UUID  (NOT NULL)
  - goal            TEXT  (NOT NULL)
  - status          VARCHAR(20)  draft|approved|rejected|executing|complete|failed
  - graph_json      JSONB (NOT NULL, serialised TaskGraph)
  - execution_plan  TEXT  (NOT NULL)
  - metadata_json   JSONB (NOT NULL, default {})
  - created_at      TIMESTAMP WITH TIME ZONE
  - updated_at      TIMESTAMP WITH TIME ZONE
  - approved_by     UUID  (nullable)
  - approved_at     TIMESTAMP WITH TIME ZONE (nullable)
  - rejected_by     UUID  (nullable)
  - rejected_at     TIMESTAMP WITH TIME ZONE (nullable)

Indexes:
  - ix_execution_plans_tenant_created   (tenant_id, created_at)
  - ix_execution_plans_tenant_status    (tenant_id, status)

Notes:
  - No FK to tenants.id — plans may outlive tenant soft-deletes.
  - No FK to users.id — same rationale (audit durability).
  - status stored as VARCHAR rather than a PostgreSQL enum for schema
    flexibility (avoids enum migration pain when adding new statuses).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create execution_plans table with indexes."""

    op.create_table(
        "execution_plans",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="Plan primary key",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning tenant — all queries must filter on this column",
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="User who created the plan",
        ),
        sa.Column(
            "goal",
            sa.Text(),
            nullable=False,
            comment="High-level goal that was decomposed",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="draft",
            comment="draft | approved | rejected | executing | complete | failed",
        ),
        sa.Column(
            "graph_json",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Serialised TaskGraph (nodes dict keyed by task_id)",
        ),
        sa.Column(
            "execution_plan",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="Human-readable execution order description",
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Arbitrary metadata (context, comments, etc.)",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp of plan creation",
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
            comment="User who approved the plan (null until approved)",
        ),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the plan was approved (null until approved)",
        ),
        sa.Column(
            "rejected_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="User who rejected the plan (null unless rejected)",
        ),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the plan was rejected (null unless rejected)",
        ),
    )

    # Primary access pattern: all plans for a tenant, newest first
    op.create_index(
        "ix_execution_plans_tenant_created",
        "execution_plans",
        ["tenant_id", "created_at"],
        comment="List plans for a tenant ordered by creation time",
    )

    # Status filter: pending approvals, executing plans, etc.
    op.create_index(
        "ix_execution_plans_tenant_status",
        "execution_plans",
        ["tenant_id", "status"],
        comment="Filter plans by status within a tenant",
    )


def downgrade() -> None:
    """Drop execution_plans table and its indexes."""

    op.drop_index("ix_execution_plans_tenant_status", table_name="execution_plans")
    op.drop_index("ix_execution_plans_tenant_created", table_name="execution_plans")
    op.drop_table("execution_plans")
