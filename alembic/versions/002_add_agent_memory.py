"""Add agent_memory table for Phase 4A

Revision ID: 002
Revises: 001
Create Date: 2026-02-16

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create agent_memory table."""
    op.create_table(
        'agent_memory',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('agent_id', sa.String(128), nullable=False, index=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('key', sa.String(256), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('access_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('metadata_json', sa.Text(), nullable=True),
    )

    # Create composite index for lookups
    op.create_index(
        'idx_agent_memory_lookup',
        'agent_memory',
        ['agent_id', 'tenant_id', 'key'],
    )

    # Create index on created_at for cleanup queries
    op.create_index(
        'idx_agent_memory_created',
        'agent_memory',
        ['created_at'],
    )


def downgrade() -> None:
    """Drop agent_memory table."""
    op.drop_index('idx_agent_memory_created', table_name='agent_memory')
    op.drop_index('idx_agent_memory_lookup', table_name='agent_memory')
    op.drop_table('agent_memory')
