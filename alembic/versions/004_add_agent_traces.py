"""Add agent_traces and agent_steps tables for playground debugging

Revision ID: 004
Revises: 003a
Create Date: 2026-02-17

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '004'
down_revision: Union[str, None] = '003a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create agent_traces and agent_steps tables."""

    # Create trace_status enum
    op.execute("CREATE TYPE trace_status AS ENUM ('running', 'completed', 'failed', 'cancelled')")

    # Create step_type enum
    op.execute("CREATE TYPE step_type AS ENUM ('observe', 'think', 'plan', 'execute', 'verify', 'tool_call', 'reasoning', 'llm_call')")

    # Create agent_traces table
    op.create_table(
        'agent_traces',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('conversation_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('agent_spec_id', sa.String(128), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.Enum('running', 'completed', 'failed', 'cancelled', name='trace_status'), nullable=False, server_default='running'),
        sa.Column('error_message', sa.Text, nullable=True),
        sa.Column('total_tokens', sa.Integer, nullable=False, server_default='0'),
        sa.Column('total_steps', sa.Integer, nullable=False, server_default='0'),
        sa.Column('input_message', sa.Text, nullable=True),
        sa.Column('output_response', sa.Text, nullable=True),
        sa.Column('metadata', postgresql.JSONB, nullable=False, server_default='{}'),

        # Foreign keys
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
    )

    # Indexes for agent_traces
    op.create_index(
        'ix_agent_traces_tenant_id',
        'agent_traces',
        ['tenant_id']
    )
    op.create_index(
        'ix_agent_traces_conversation_id',
        'agent_traces',
        ['conversation_id']
    )
    op.create_index(
        'ix_agent_traces_agent_spec_id',
        'agent_traces',
        ['agent_spec_id']
    )
    op.create_index(
        'ix_agent_traces_tenant_started',
        'agent_traces',
        ['tenant_id', 'started_at']
    )
    op.create_index(
        'ix_agent_traces_tenant_agent',
        'agent_traces',
        ['tenant_id', 'agent_spec_id']
    )
    op.create_index(
        'ix_agent_traces_status',
        'agent_traces',
        ['status']
    )

    # Create agent_steps table
    op.create_table(
        'agent_steps',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('trace_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('step_number', sa.Integer, nullable=False),
        sa.Column('step_type', sa.Enum('observe', 'think', 'plan', 'execute', 'verify', 'tool_call', 'reasoning', 'llm_call', name='step_type'), nullable=False),
        sa.Column('input_data', postgresql.JSONB, nullable=False, server_default='{}'),
        sa.Column('output_data', postgresql.JSONB, nullable=False, server_default='{}'),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('token_count', sa.Integer, nullable=False, server_default='0'),
        sa.Column('model_used', sa.String(128), nullable=True),
        sa.Column('metadata', postgresql.JSONB, nullable=False, server_default='{}'),

        # Foreign keys
        sa.ForeignKeyConstraint(['trace_id'], ['agent_traces.id'], ondelete='CASCADE'),
    )

    # Indexes for agent_steps
    op.create_index(
        'ix_agent_steps_trace_id',
        'agent_steps',
        ['trace_id']
    )
    op.create_index(
        'ix_agent_steps_trace_step',
        'agent_steps',
        ['trace_id', 'step_number']
    )
    op.create_index(
        'ix_agent_steps_type',
        'agent_steps',
        ['step_type']
    )


def downgrade() -> None:
    """Drop agent_traces and agent_steps tables."""
    # Drop indexes for agent_steps
    op.drop_index('ix_agent_steps_type', table_name='agent_steps')
    op.drop_index('ix_agent_steps_trace_step', table_name='agent_steps')
    op.drop_index('ix_agent_steps_trace_id', table_name='agent_steps')

    # Drop agent_steps table
    op.drop_table('agent_steps')

    # Drop indexes for agent_traces
    op.drop_index('ix_agent_traces_status', table_name='agent_traces')
    op.drop_index('ix_agent_traces_tenant_agent', table_name='agent_traces')
    op.drop_index('ix_agent_traces_tenant_started', table_name='agent_traces')
    op.drop_index('ix_agent_traces_agent_spec_id', table_name='agent_traces')
    op.drop_index('ix_agent_traces_conversation_id', table_name='agent_traces')
    op.drop_index('ix_agent_traces_tenant_id', table_name='agent_traces')

    # Drop agent_traces table
    op.drop_table('agent_traces')

    # Drop enums
    op.execute("DROP TYPE step_type")
    op.execute("DROP TYPE trace_status")
