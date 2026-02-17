"""Add compliance_runs and compliance_findings tables for Phase 9C.

Revision ID: 011
Revises: 010
Create Date: 2026-02-17

Phase 9C: Audit & Compliance Automation

Adds:
- compliance_runs table
  - id UUID PK
  - tenant_id UUID FK -> tenants.id CASCADE
  - triggered_by VARCHAR(32)  "scheduled" | "manual"
  - started_at TIMESTAMP WITH TIME ZONE
  - completed_at TIMESTAMP WITH TIME ZONE (nullable)
  - overall_status VARCHAR(16)  "PASS" | "FAIL" | "WARNING" | "SKIP"
  - score FLOAT  0.0-100.0
  - report JSONB  full ComplianceReport serialised

- compliance_findings table
  - id UUID PK
  - run_id UUID FK -> compliance_runs.id CASCADE
  - check_name VARCHAR(64)
  - status VARCHAR(16)
  - details TEXT
  - evidence JSONB
  - remediation_suggestion TEXT (nullable)

Indexes:
- ix_compliance_runs_tenant_started      (tenant_id, started_at)
- ix_compliance_runs_tenant_status       (tenant_id, overall_status)
- ix_compliance_findings_run_check       (run_id, check_name)
- ix_compliance_findings_run             (run_id)

Notes:
- No enum types; status values stored as VARCHAR for schema flexibility.
- Cascade deletes on both FK constraints: removing a tenant removes all
  compliance history; removing a run removes all its findings.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create compliance_runs and compliance_findings tables."""

    # ------------------------------------------------------------------
    # compliance_runs
    # ------------------------------------------------------------------
    op.create_table(
        "compliance_runs",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Tenant this run belongs to",
        ),
        sa.Column(
            "triggered_by",
            sa.String(32),
            nullable=False,
            server_default="manual",
            comment="scheduled | manual",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="UTC timestamp when the run started",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC timestamp when the run finished (NULL while in progress)",
        ),
        sa.Column(
            "overall_status",
            sa.String(16),
            nullable=False,
            server_default="SKIP",
            comment="PASS | FAIL | WARNING | SKIP",
        ),
        sa.Column(
            "score",
            sa.Float(),
            nullable=False,
            server_default="0.0",
            comment="Compliance score 0.0-100.0",
        ),
        sa.Column(
            "report",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Full serialised ComplianceReport JSON",
        ),

        # Foreign key
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_compliance_runs_tenant_id",
            ondelete="CASCADE",
        ),
    )

    # Indexes for compliance_runs
    op.create_index(
        "ix_compliance_runs_tenant_started",
        "compliance_runs",
        ["tenant_id", "started_at"],
        comment="Primary query pattern: list runs for tenant ordered by time",
    )
    op.create_index(
        "ix_compliance_runs_tenant_status",
        "compliance_runs",
        ["tenant_id", "overall_status"],
        comment="Filter runs by compliance status for dashboards",
    )

    # ------------------------------------------------------------------
    # compliance_findings
    # ------------------------------------------------------------------
    op.create_table(
        "compliance_findings",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Compliance run this finding belongs to",
        ),
        sa.Column(
            "check_name",
            sa.String(64),
            nullable=False,
            comment="e.g. data_classification, access_controls",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="PASS | FAIL | WARNING | SKIP",
        ),
        sa.Column(
            "details",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="Human-readable description of the check result",
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Structured evidence data supporting the finding",
        ),
        sa.Column(
            "remediation_suggestion",
            sa.Text(),
            nullable=True,
            comment="Actionable remediation steps for non-PASS findings",
        ),

        # Foreign key
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["compliance_runs.id"],
            name="fk_compliance_findings_run_id",
            ondelete="CASCADE",
        ),
    )

    # Indexes for compliance_findings
    op.create_index(
        "ix_compliance_findings_run",
        "compliance_findings",
        ["run_id"],
        comment="All findings for a run",
    )
    op.create_index(
        "ix_compliance_findings_run_check",
        "compliance_findings",
        ["run_id", "check_name"],
        comment="Specific check within a run",
    )
    op.create_index(
        "ix_compliance_findings_check_status",
        "compliance_findings",
        ["check_name", "status"],
        comment="Cross-run analysis: trend of a specific check",
    )


def downgrade() -> None:
    """Drop compliance_findings and compliance_runs tables."""

    # Drop findings first (FK dependency)
    op.drop_index("ix_compliance_findings_check_status", table_name="compliance_findings")
    op.drop_index("ix_compliance_findings_run_check", table_name="compliance_findings")
    op.drop_index("ix_compliance_findings_run", table_name="compliance_findings")
    op.drop_table("compliance_findings")

    # Drop runs
    op.drop_index("ix_compliance_runs_tenant_status", table_name="compliance_runs")
    op.drop_index("ix_compliance_runs_tenant_started", table_name="compliance_runs")
    op.drop_table("compliance_runs")
