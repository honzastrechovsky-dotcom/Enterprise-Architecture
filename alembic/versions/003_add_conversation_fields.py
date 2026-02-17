"""Add is_archived and token_count fields to conversations.

Revision ID: 003a
Revises: 002
Create Date: 2026-02-17

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '003a'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_archived to conversations and token_count to messages."""
    # Add is_archived column to conversations table
    op.add_column(
        'conversations',
        sa.Column(
            'is_archived',
            sa.Boolean(),
            nullable=False,
            server_default='false',
            comment='Archived conversations are hidden from default list view',
        )
    )

    # Add token_count column to messages table
    op.add_column(
        'messages',
        sa.Column(
            'token_count',
            sa.Integer(),
            nullable=True,
        )
    )

    # Create index on is_archived for efficient filtering
    op.create_index(
        'ix_conversations_archived',
        'conversations',
        ['is_archived']
    )


def downgrade() -> None:
    """Remove is_archived from conversations and token_count from messages."""
    op.drop_index('ix_conversations_archived', table_name='conversations')
    op.drop_column('conversations', 'is_archived')
    op.drop_column('messages', 'token_count')
