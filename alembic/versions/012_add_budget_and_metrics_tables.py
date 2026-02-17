"""Add token_budgets, token_usage_records, and routing_decisions tables for Phase 7B.

Revision ID: 012
Revises: 011
Create Date: 2026-02-17

Phase 7B: Persist In-Memory Stores

Adds:
- token_budgets table (one row per tenant, mutable)
  - id UUID PK
  - tenant_id UUID UNIQUE  — one budget per tenant
  - daily_limit INTEGER
  - monthly_limit INTEGER
  - current_daily INTEGER
  - current_monthly INTEGER
  - last_reset_date VARCHAR(10)   YYYY-MM-DD
  - last_reset_month VARCHAR(7)   YYYY-MM
  - updated_at TIMESTAMP WITH TIME ZONE

- token_usage_records table (append-only)
  - id UUID PK
  - tenant_id UUID
  - timestamp TIMESTAMP WITH TIME ZONE
  - model_tier VARCHAR(20)
  - input_tokens INTEGER
  - output_tokens INTEGER
  - total_tokens INTEGER
  - complexity_score FLOAT

- routing_decisions table (append-only)
  - id UUID PK
  - tenant_id UUID
  - timestamp TIMESTAMP WITH TIME ZONE
  - task_type VARCHAR(50)
  - selected_tier VARCHAR(20)
  - estimated_complexity FLOAT
  - actual_quality FLOAT (nullable)
  - tokens_used INTEGER
  - latency_ms FLOAT
  - metadata_json TEXT (nullable)

Indexes:
- ix_token_budgets_tenant             (tenant_id, unique)
- ix_token_usage_tenant_time          (tenant_id, timestamp)
- ix_routing_decisions_tenant_time    (tenant_id, timestamp)

Notes:
- No foreign keys to tenants — budget/metrics subsystem is intentionally
  decoupled for operational resilience.
- token_budgets uses no auto-update trigger for updated_at; the application
  layer sets the value on each write. This avoids a PostgreSQL trigger
  dependency while keeping the column useful for debugging.
- SELECT ... FOR UPDATE must be used by application code when updating
  token_budgets to prevent lost-update races under concurrent requests.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create token_budgets, token_usage_records, and routing_decisions tables."""

    # ------------------------------------------------------------------
    # token_budgets — one mutable row per tenant
    # ------------------------------------------------------------------
    op.create_table(
        "token_budgets",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="Surrogate primary key",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Tenant this budget belongs to (unique — one budget per tenant)",
        ),
        sa.Column(
            "daily_limit",
            sa.Integer(),
            nullable=False,
            server_default="1000000",
            comment="Maximum tokens allowed per calendar day (UTC)",
        ),
        sa.Column(
            "monthly_limit",
            sa.Integer(),
            nullable=False,
            server_default="20000000",
            comment="Maximum tokens allowed per calendar month (UTC)",
        ),
        sa.Column(
            "current_daily",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Tokens consumed since last_reset_date",
        ),
        sa.Column(
            "current_monthly",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Tokens consumed since last_reset_month",
        ),
        sa.Column(
            "last_reset_date",
            sa.String(10),
            nullable=False,
            server_default="",
            comment="YYYY-MM-DD of last daily counter reset",
        ),
        sa.Column(
            "last_reset_month",
            sa.String(7),
            nullable=False,
            server_default="",
            comment="YYYY-MM of last monthly counter reset",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp of last modification",
        ),
    )

    # Unique index on tenant_id — lookup key for get-or-create logic
    op.create_index(
        "ix_token_budgets_tenant",
        "token_budgets",
        ["tenant_id"],
        unique=True,
        comment="One budget row per tenant; used as the primary lookup key",
    )

    # ------------------------------------------------------------------
    # token_usage_records — append-only usage event log
    # ------------------------------------------------------------------
    op.create_table(
        "token_usage_records",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="Surrogate primary key",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Tenant this usage event belongs to",
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp when the tokens were consumed",
        ),
        sa.Column(
            "model_tier",
            sa.String(20),
            nullable=False,
            comment="Model tier used: light | standard | heavy",
        ),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            nullable=False,
            comment="Prompt tokens for this request",
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            nullable=False,
            comment="Completion tokens for this request",
        ),
        sa.Column(
            "total_tokens",
            sa.Integer(),
            nullable=False,
            comment="input_tokens + output_tokens",
        ),
        sa.Column(
            "complexity_score",
            sa.Float(),
            nullable=False,
            server_default="0.0",
            comment="Estimated task complexity score (0.0-1.0) at time of routing",
        ),
    )

    # Composite index for the primary read pattern: usage window for a tenant
    op.create_index(
        "ix_token_usage_tenant_time",
        "token_usage_records",
        ["tenant_id", "timestamp"],
        comment="Primary query pattern: usage events for tenant within time window",
    )

    # ------------------------------------------------------------------
    # routing_decisions — append-only routing decision log
    # ------------------------------------------------------------------
    op.create_table(
        "routing_decisions",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="Surrogate primary key",
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Tenant this routing decision belongs to",
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp of the routing decision",
        ),
        sa.Column(
            "task_type",
            sa.String(50),
            nullable=False,
            comment="Task classifier category (e.g. reasoning, search, summarization)",
        ),
        sa.Column(
            "selected_tier",
            sa.String(20),
            nullable=False,
            comment="Model tier selected: light | standard | heavy",
        ),
        sa.Column(
            "estimated_complexity",
            sa.Float(),
            nullable=False,
            comment="Complexity score (0.0-1.0) used to select the tier",
        ),
        sa.Column(
            "actual_quality",
            sa.Float(),
            nullable=True,
            comment="Measured response quality (0.0-1.0); NULL until scored",
        ),
        sa.Column(
            "tokens_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Total tokens consumed by this request",
        ),
        sa.Column(
            "latency_ms",
            sa.Float(),
            nullable=False,
            server_default="0.0",
            comment="End-to-end latency in milliseconds",
        ),
        sa.Column(
            "metadata_json",
            sa.Text(),
            nullable=True,
            comment="JSON-encoded arbitrary routing metadata",
        ),
    )

    # Composite index for the primary read pattern: decisions window for a tenant
    op.create_index(
        "ix_routing_decisions_tenant_time",
        "routing_decisions",
        ["tenant_id", "timestamp"],
        comment="Primary query pattern: decisions for tenant within time window",
    )


def downgrade() -> None:
    """Drop routing_decisions, token_usage_records, and token_budgets tables."""

    # Drop in reverse creation order to respect dependency clarity
    op.drop_index("ix_routing_decisions_tenant_time", table_name="routing_decisions")
    op.drop_table("routing_decisions")

    op.drop_index("ix_token_usage_tenant_time", table_name="token_usage_records")
    op.drop_table("token_usage_records")

    op.drop_index("ix_token_budgets_tenant", table_name="token_budgets")
    op.drop_table("token_budgets")
