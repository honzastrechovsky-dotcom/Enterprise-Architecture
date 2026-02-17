"""Add user_goals table for Phase 11E persistent goal tracking.

Revision ID: 017
Revises: 016
Create Date: 2026-02-17

Phase 11E: Persistent user goal tracking so the agent can follow long-running
goals across multiple conversations.  Adds the user_goals table which backs
GoalService and allows goals to survive restarts and work across multiple
API instances.

Table: user_goals
  - id              UUID PK
  - tenant_id       UUID  (NOT NULL — tenant isolation)
  - user_id         UUID  (NOT NULL — goal owner)
  - goal_text       TEXT  (NOT NULL — free-text goal description)
  - status          VARCHAR(50)  active|completed|abandoned  (default: active)
  - progress_notes  TEXT  (nullable — accumulated progress notes from agent)
  - created_at      TIMESTAMP WITH TIME ZONE (NOT NULL)
  - updated_at      TIMESTAMP WITH TIME ZONE (NOT NULL)
  - completed_at    TIMESTAMP WITH TIME ZONE (nullable)

Indexes:
  - idx_user_goals_tenant_user  (tenant_id, user_id)
  - idx_user_goals_status       (status)

Notes:
  - No FK to tenants.id or users.id — goals must outlive soft-deletes
    for audit durability (same pattern as write_operations).
  - status stored as VARCHAR to avoid PostgreSQL enum migration pain.
  - progress_notes is appended by the agent; users manually complete goals.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create user_goals table with indexes."""

    op.create_table(
        "user_goals",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="User goal primary key",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Owning tenant — all queries must filter on this column",
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="User who owns this goal",
        ),
        sa.Column(
            "goal_text",
            sa.Text(),
            nullable=False,
            comment="Free-text description of the goal",
        ),
        sa.Column(
            "status",
            sa.String(50),
            nullable=False,
            server_default="active",
            comment="active | completed | abandoned",
        ),
        sa.Column(
            "progress_notes",
            sa.Text(),
            nullable=True,
            comment="Accumulated progress notes from agent conversations",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp of goal creation",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp of last modification",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC timestamp when goal was completed",
        ),
    )

    # Primary lookup: all goals for a user within a tenant
    op.create_index(
        "idx_user_goals_tenant_user",
        "user_goals",
        ["tenant_id", "user_id"],
        comment="List goals for a user within a tenant",
    )

    # Status filtering across the tenant
    op.create_index(
        "idx_user_goals_status",
        "user_goals",
        ["status"],
        comment="Filter goals by status",
    )


def downgrade() -> None:
    """Drop user_goals table and its indexes."""

    op.drop_index("idx_user_goals_status", table_name="user_goals")
    op.drop_index("idx_user_goals_tenant_user", table_name="user_goals")
    op.drop_table("user_goals")
