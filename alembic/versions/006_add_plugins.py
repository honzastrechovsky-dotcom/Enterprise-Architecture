"""Add plugin_registrations table for plugin system.

Revision ID: 006
Revises: 005
Create Date: 2026-02-17

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '006'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create plugin_registrations table."""
    op.create_table(
        'plugin_registrations',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('plugin_name', sa.String(255), nullable=False),
        sa.Column('plugin_version', sa.String(64), nullable=False),
        sa.Column('enabled', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('config', postgresql.JSONB, nullable=False, server_default='{}'),
        sa.Column(
            'installed_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now()
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now()
        ),

        # Foreign keys
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    )

    # Indexes
    op.create_index(
        'ix_plugin_registrations_tenant_id',
        'plugin_registrations',
        ['tenant_id']
    )
    op.create_index(
        'uq_plugin_tenant',
        'plugin_registrations',
        ['tenant_id', 'plugin_name'],
        unique=True
    )
    op.create_index(
        'ix_plugin_enabled',
        'plugin_registrations',
        ['tenant_id', 'enabled']
    )


def downgrade() -> None:
    """Drop plugin_registrations table."""
    op.drop_index('ix_plugin_enabled', table_name='plugin_registrations')
    op.drop_index('uq_plugin_tenant', table_name='plugin_registrations')
    op.drop_index('ix_plugin_registrations_tenant_id', table_name='plugin_registrations')
    op.drop_table('plugin_registrations')
