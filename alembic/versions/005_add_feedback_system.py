"""Add feedback and fine-tuning tables for Phase 6A

Revision ID: 005
Revises: 004
Create Date: 2026-02-17

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '005'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create feedback and fine-tuning tables."""

    # Create feedback_rating enum
    op.execute("""
        CREATE TYPE feedback_rating AS ENUM (
            'thumbs_up', 'thumbs_down',
            'rating_1', 'rating_2', 'rating_3', 'rating_4', 'rating_5'
        )
    """)

    # Create dataset_status enum
    op.execute("""
        CREATE TYPE dataset_status AS ENUM ('draft', 'ready', 'exported')
    """)

    # Create response_feedback table
    op.create_table(
        'response_feedback',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('conversation_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('message_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('trace_id', sa.String(128), nullable=True),
        sa.Column('rating', sa.Enum(
            'thumbs_up', 'thumbs_down',
            'rating_1', 'rating_2', 'rating_3', 'rating_4', 'rating_5',
            name='feedback_rating'
        ), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('tags', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('prompt_text', sa.Text(), nullable=False),
        sa.Column('response_text', sa.Text(), nullable=False),
        sa.Column('model_used', sa.String(128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='SET NULL'),
    )

    # Create indexes for response_feedback
    op.create_index('ix_response_feedback_tenant_id', 'response_feedback', ['tenant_id'])
    op.create_index('ix_response_feedback_user_id', 'response_feedback', ['user_id'])
    op.create_index('ix_response_feedback_conversation_id', 'response_feedback', ['conversation_id'])
    op.create_index('ix_response_feedback_message_id', 'response_feedback', ['message_id'])
    op.create_index('ix_response_feedback_trace_id', 'response_feedback', ['trace_id'])
    op.create_index('ix_response_feedback_rating', 'response_feedback', ['rating'])
    op.create_index('ix_response_feedback_model_used', 'response_feedback', ['model_used'])
    op.create_index('ix_response_feedback_created_at', 'response_feedback', ['created_at'])
    op.create_index('ix_feedback_tenant_rating', 'response_feedback', ['tenant_id', 'rating'])
    op.create_index('ix_feedback_tenant_created', 'response_feedback', ['tenant_id', 'created_at'])
    op.create_index('ix_feedback_tenant_model', 'response_feedback', ['tenant_id', 'model_used'])

    # Create finetuning_datasets table
    op.create_table(
        'finetuning_datasets',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.Enum('draft', 'ready', 'exported', name='dataset_status'), nullable=False, server_default='draft'),
        sa.Column('record_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('filters', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    )

    # Create indexes for finetuning_datasets
    op.create_index('ix_finetuning_datasets_tenant_id', 'finetuning_datasets', ['tenant_id'])
    op.create_index('ix_finetuning_datasets_status', 'finetuning_datasets', ['status'])
    op.create_index('ix_datasets_tenant_status', 'finetuning_datasets', ['tenant_id', 'status'])
    op.create_index('ix_datasets_tenant_created', 'finetuning_datasets', ['tenant_id', 'created_at'])

    # Create finetuning_records table
    op.create_table(
        'finetuning_records',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('dataset_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('feedback_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('system_prompt', sa.Text(), nullable=False, server_default='You are a helpful assistant.'),
        sa.Column('user_prompt', sa.Text(), nullable=False),
        sa.Column('assistant_response', sa.Text(), nullable=False),
        sa.Column('quality_score', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('included', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['dataset_id'], ['finetuning_datasets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['feedback_id'], ['response_feedback.id'], ondelete='CASCADE'),
    )

    # Create indexes for finetuning_records
    op.create_index('ix_finetuning_records_dataset_id', 'finetuning_records', ['dataset_id'])
    op.create_index('ix_finetuning_records_feedback_id', 'finetuning_records', ['feedback_id'])
    op.create_index('ix_records_dataset_included', 'finetuning_records', ['dataset_id', 'included'])
    op.create_index('ix_records_feedback', 'finetuning_records', ['feedback_id'])


def downgrade() -> None:
    """Drop feedback and fine-tuning tables."""
    op.drop_index('ix_records_feedback', table_name='finetuning_records')
    op.drop_index('ix_records_dataset_included', table_name='finetuning_records')
    op.drop_index('ix_finetuning_records_feedback_id', table_name='finetuning_records')
    op.drop_index('ix_finetuning_records_dataset_id', table_name='finetuning_records')
    op.drop_table('finetuning_records')

    op.drop_index('ix_datasets_tenant_created', table_name='finetuning_datasets')
    op.drop_index('ix_datasets_tenant_status', table_name='finetuning_datasets')
    op.drop_index('ix_finetuning_datasets_status', table_name='finetuning_datasets')
    op.drop_index('ix_finetuning_datasets_tenant_id', table_name='finetuning_datasets')
    op.drop_table('finetuning_datasets')

    op.drop_index('ix_feedback_tenant_model', table_name='response_feedback')
    op.drop_index('ix_feedback_tenant_created', table_name='response_feedback')
    op.drop_index('ix_feedback_tenant_rating', table_name='response_feedback')
    op.drop_index('ix_response_feedback_created_at', table_name='response_feedback')
    op.drop_index('ix_response_feedback_model_used', table_name='response_feedback')
    op.drop_index('ix_response_feedback_rating', table_name='response_feedback')
    op.drop_index('ix_response_feedback_trace_id', table_name='response_feedback')
    op.drop_index('ix_response_feedback_message_id', table_name='response_feedback')
    op.drop_index('ix_response_feedback_conversation_id', table_name='response_feedback')
    op.drop_index('ix_response_feedback_user_id', table_name='response_feedback')
    op.drop_index('ix_response_feedback_tenant_id', table_name='response_feedback')
    op.drop_table('response_feedback')

    op.execute("DROP TYPE dataset_status")
    op.execute("DROP TYPE feedback_rating")
