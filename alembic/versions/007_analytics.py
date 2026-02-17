"""Add analytics tables for usage tracking.

Revision ID: 007
Revises: 006
Create Date: 2026-02-17

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '007'
down_revision: Union[str, None] = '006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create analytics tables."""

    # Create metric_type enum
    op.execute("CREATE TYPE metric_type AS ENUM ('api_call', 'token_usage', 'agent_run', 'tool_call', 'document_query')")

    # Create usage_metrics table
    op.create_table(
        'usage_metrics',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('metric_type', sa.Enum('api_call', 'token_usage', 'agent_run', 'tool_call', 'document_query', name='metric_type'), nullable=False),
        sa.Column('value', sa.Float, nullable=False),
        sa.Column('dimensions', postgresql.JSONB, nullable=False, server_default='{}'),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),

        # Foreign keys
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    )

    # Create indexes for usage_metrics
    op.create_index('ix_usage_metrics_tenant_id', 'usage_metrics', ['tenant_id'])
    op.create_index('ix_usage_metrics_metric_type', 'usage_metrics', ['metric_type'])
    op.create_index('ix_usage_metrics_timestamp', 'usage_metrics', ['timestamp'])
    op.create_index('ix_usage_metrics_tenant_timestamp', 'usage_metrics', ['tenant_id', 'timestamp'])
    op.create_index('ix_usage_metrics_tenant_type', 'usage_metrics', ['tenant_id', 'metric_type'])
    op.create_index('ix_usage_metrics_tenant_type_timestamp', 'usage_metrics', ['tenant_id', 'metric_type', 'timestamp'])

    # Create daily_summaries table
    op.create_table(
        'daily_summaries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('date', sa.Date, nullable=False),
        sa.Column('total_api_calls', sa.Integer, nullable=False, server_default='0'),
        sa.Column('total_tokens', sa.Integer, nullable=False, server_default='0'),
        sa.Column('total_agent_runs', sa.Integer, nullable=False, server_default='0'),
        sa.Column('unique_users', sa.Integer, nullable=False, server_default='0'),
        sa.Column('avg_response_time_ms', sa.Float, nullable=False, server_default='0.0'),
        sa.Column('error_count', sa.Integer, nullable=False, server_default='0'),
        sa.Column('cost_estimate', sa.Float, nullable=False, server_default='0.0'),
        sa.Column('dimensions', postgresql.JSONB, nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),

        # Foreign keys
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    )

    # Create indexes for daily_summaries
    op.create_index('ix_daily_summaries_tenant_id', 'daily_summaries', ['tenant_id'])
    op.create_index('ix_daily_summaries_date', 'daily_summaries', ['date'])
    op.create_index('ix_daily_summaries_tenant_date', 'daily_summaries', ['tenant_id', 'date'], unique=True)


def downgrade() -> None:
    """Drop analytics tables."""
    # Drop daily_summaries
    op.drop_index('ix_daily_summaries_tenant_date', table_name='daily_summaries')
    op.drop_index('ix_daily_summaries_date', table_name='daily_summaries')
    op.drop_index('ix_daily_summaries_tenant_id', table_name='daily_summaries')
    op.drop_table('daily_summaries')

    # Drop usage_metrics
    op.drop_index('ix_usage_metrics_tenant_type_timestamp', table_name='usage_metrics')
    op.drop_index('ix_usage_metrics_tenant_type', table_name='usage_metrics')
    op.drop_index('ix_usage_metrics_tenant_timestamp', table_name='usage_metrics')
    op.drop_index('ix_usage_metrics_timestamp', table_name='usage_metrics')
    op.drop_index('ix_usage_metrics_metric_type', table_name='usage_metrics')
    op.drop_index('ix_usage_metrics_tenant_id', table_name='usage_metrics')
    op.drop_table('usage_metrics')

    # Drop enum
    op.execute("DROP TYPE metric_type")
