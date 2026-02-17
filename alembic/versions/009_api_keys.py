"""Add api_keys table for programmatic authentication.

Revision ID: 009
Revises: 008
Create Date: 2026-02-17

Phase 9D: API Key Management

Adds:
- api_keys table - stores hashed API keys with metadata
  - Secure key storage: only SHA-256 hash stored, never raw key
  - Scoped access via JSONB scopes array
  - Per-key rate limiting support
  - Expiration and revocation support
  - Tenant isolation enforced via tenant_id FK

Indexes:
- ix_api_keys_key_hash - fast lookup during authentication
- ix_api_keys_tenant_id - listing keys per tenant
- ix_api_keys_tenant_active - listing active keys per tenant
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create api_keys table with indexes."""

    op.create_table(
        "api_keys",
        # Primary key
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),

        # Tenant scoping
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),

        # Metadata
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),

        # Security - only hash stored, never raw key
        sa.Column(
            "key_hash",
            sa.String(64),
            nullable=False,
            unique=True,
            comment="SHA-256 hex digest of the raw key",
        ),
        sa.Column(
            "key_prefix",
            sa.String(8),
            nullable=False,
            comment="First 8 chars of raw key for human identification",
        ),

        # Authorization
        sa.Column(
            "scopes",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
            comment="List of allowed scopes, e.g. [\"chat\", \"documents\"]",
        ),

        # Rate limiting
        sa.Column(
            "rate_limit_per_minute",
            sa.Integer(),
            nullable=True,
            comment="Max requests per minute (NULL = unlimited)",
        ),

        # Lifecycle
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Expiration timestamp (NULL = never expires)",
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Last authentication timestamp",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default="true",
            comment="Whether the key can be used for authentication",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the key was revoked (NULL = not revoked)",
        ),

        # Foreign keys
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_api_keys_tenant_id",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_api_keys_created_by",
        ),
    )

    # Performance indexes
    op.create_index(
        "ix_api_keys_key_hash",
        "api_keys",
        ["key_hash"],
        unique=True,
        comment="Fast lookup during API key authentication",
    )
    op.create_index(
        "ix_api_keys_tenant_id",
        "api_keys",
        ["tenant_id"],
        comment="List keys per tenant",
    )
    op.create_index(
        "ix_api_keys_tenant_active",
        "api_keys",
        ["tenant_id", "is_active"],
        comment="List active keys per tenant",
    )


def downgrade() -> None:
    """Drop api_keys table and indexes."""
    op.drop_index("ix_api_keys_tenant_active", table_name="api_keys")
    op.drop_index("ix_api_keys_tenant_id", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
