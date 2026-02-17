"""Add fine_tuning_jobs table for Phase 10A job queue persistence.

Revision ID: 015
Revises: 014
Create Date: 2026-02-17

Phase 10A: Replace in-memory job tracking with PostgreSQL persistence.
Adds the fine_tuning_jobs table which backs PersistentFineTuningManager
and allows training jobs to survive restarts and work across multiple
API instances.

Table: fine_tuning_jobs
  - id                UUID PK
  - tenant_id         UUID  (NOT NULL, indexed, tenant isolation)
  - user_id           UUID  (NOT NULL — user who created the job)
  - base_model        VARCHAR(200)  (NOT NULL — LiteLLM model identifier)
  - dataset_path      TEXT  (NOT NULL — path to training JSONL)
  - output_path       TEXT  (NOT NULL — path to save model/adapter)
  - hyperparameters   JSONB (NOT NULL, default {} — epochs, lr, lora_rank, etc.)
  - status            VARCHAR(30)   pending|preparing|training|evaluating|
                                    completed|failed|cancelled
  - progress          FLOAT (NOT NULL, default 0.0 — 0-100 percent)
  - metrics           JSONB (NOT NULL, default {} — accumulated training metrics)
  - evaluation_json   JSONB (nullable — evaluation results after evaluate_model())
  - error_message     TEXT  (nullable — error detail when status=failed)
  - created_at        TIMESTAMP WITH TIME ZONE (NOT NULL)
  - started_at        TIMESTAMP WITH TIME ZONE (nullable)
  - completed_at      TIMESTAMP WITH TIME ZONE (nullable)
  - updated_at        TIMESTAMP WITH TIME ZONE (NOT NULL)

Indexes:
  - ix_fine_tuning_jobs_tenant_created  (tenant_id, created_at)
  - ix_fine_tuning_jobs_tenant_status   (tenant_id, status)

Notes:
  - No FK to tenants.id or users.id — jobs must outlive soft-deletes
    for audit durability (same pattern as write_operations).
  - status stored as VARCHAR to avoid PostgreSQL enum migration pain.
  - hyperparameters column preserves all FineTuningConfig fields without
    requiring dedicated columns for each (forward compatible).
  - Token counts are stored inside metrics once calculated from the dataset;
    they are NOT hardcoded values.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create fine_tuning_jobs table with indexes."""

    op.create_table(
        "fine_tuning_jobs",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            comment="Fine-tuning job primary key",
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
            comment="User who created the job",
        ),
        sa.Column(
            "base_model",
            sa.String(200),
            nullable=False,
            comment="Base model identifier in LiteLLM format",
        ),
        sa.Column(
            "dataset_path",
            sa.Text(),
            nullable=False,
            comment="Filesystem path to training dataset JSONL",
        ),
        sa.Column(
            "output_path",
            sa.Text(),
            nullable=False,
            comment="Filesystem path where fine-tuned model/adapter will be saved",
        ),
        sa.Column(
            "hyperparameters",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Training hyperparameters (epochs, learning_rate, lora_rank, etc.)",
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="pending",
            comment="pending | preparing | training | evaluating | completed | failed | cancelled",
        ),
        sa.Column(
            "progress",
            sa.Float(),
            nullable=False,
            server_default="0.0",
            comment="Training progress percentage (0-100)",
        ),
        sa.Column(
            "metrics",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Training metrics accumulated during the run",
        ),
        sa.Column(
            "evaluation_json",
            postgresql.JSONB,
            nullable=True,
            comment="Evaluation results from evaluate_model(); null until evaluated",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="Human-readable error description when status=failed",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp of job creation",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC timestamp when training preparation began",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC timestamp when the job reached a terminal state",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp of last modification",
        ),
    )

    # Primary list query: all jobs for a tenant ordered by recency
    op.create_index(
        "ix_fine_tuning_jobs_tenant_created",
        "fine_tuning_jobs",
        ["tenant_id", "created_at"],
        comment="List fine-tuning jobs for a tenant ordered by creation time",
    )

    # Status filtering: list jobs by status within a tenant
    op.create_index(
        "ix_fine_tuning_jobs_tenant_status",
        "fine_tuning_jobs",
        ["tenant_id", "status"],
        comment="Filter fine-tuning jobs by status within a tenant",
    )


def downgrade() -> None:
    """Drop fine_tuning_jobs table and its indexes."""

    op.drop_index("ix_fine_tuning_jobs_tenant_status", table_name="fine_tuning_jobs")
    op.drop_index("ix_fine_tuning_jobs_tenant_created", table_name="fine_tuning_jobs")
    op.drop_table("fine_tuning_jobs")
