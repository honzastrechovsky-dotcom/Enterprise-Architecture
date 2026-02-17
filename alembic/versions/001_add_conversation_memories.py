"""Add conversation_memories table for RAG memory extraction.

Revision ID: 001
Revises:
Create Date: 2026-02-16

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create conversation_memories table."""
    op.create_table(
        'conversation_memories',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('category', sa.String(64), nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('confidence', sa.Float, nullable=False, server_default='0.5'),
        sa.Column('source_conversation_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('metadata', postgresql.JSONB, nullable=False, server_default='{}'),

        # Foreign keys
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    )

    # Indexes for efficient querying
    op.create_index(
        'ix_conversation_memories_user_tenant',
        'conversation_memories',
        ['user_id', 'tenant_id']
    )
    op.create_index(
        'ix_conversation_memories_tenant',
        'conversation_memories',
        ['tenant_id']
    )
    op.create_index(
        'ix_conversation_memories_category',
        'conversation_memories',
        ['category']
    )
    op.create_index(
        'ix_conversation_memories_expires_at',
        'conversation_memories',
        ['expires_at']
    )


def downgrade() -> None:
    """Drop conversation_memories table."""
    op.drop_index('ix_conversation_memories_expires_at', table_name='conversation_memories')
    op.drop_index('ix_conversation_memories_category', table_name='conversation_memories')
    op.drop_index('ix_conversation_memories_tenant', table_name='conversation_memories')
    op.drop_index('ix_conversation_memories_user_tenant', table_name='conversation_memories')
    op.drop_table('conversation_memories')
