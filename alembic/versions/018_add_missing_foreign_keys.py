"""Add missing foreign key constraints to user_goals and fine_tuning_jobs.

Revision ID: 018_add_missing_fks
Revises: 017_user_goals
Create Date: 2026-02-17
"""
from alembic import op

revision = "018_add_missing_fks"
down_revision = "017_user_goals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # user_goals: add FK to tenants
    op.create_foreign_key(
        "fk_user_goals_tenant_id",
        "user_goals",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # fine_tuning_jobs: add FK to tenants
    op.create_foreign_key(
        "fk_fine_tuning_jobs_tenant_id",
        "fine_tuning_jobs",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_fine_tuning_jobs_tenant_id", "fine_tuning_jobs", type_="foreignkey")
    op.drop_constraint("fk_user_goals_tenant_id", "user_goals", type_="foreignkey")
