"""Add agent_memories table for Phase 8B Agent Memory & Learning.

Revision ID: 010
Revises: 009
Create Date: 2026-02-17

Phase 8B: Agent Memory & Learning

Adds:
- agent_memories table - per-agent persistent memory store
  - memory_type enum: fact, preference, skill, context, episodic
  - content text - the memory text
  - embedding vector(1536) - for semantic similarity search (pgvector)
  - importance_score float 0.0-1.0 - decay target
  - access_count int - LRU tracking
  - last_accessed_at timestamp - decay reference point
  - expires_at nullable timestamp - optional TTL
  - metadata JSONB - freeform key-value metadata
  - is_deleted bool - soft-delete flag
  - tenant_id FK -> tenants.id CASCADE
  - created_at / updated_at timestamps

Indexes:
- ix_agent_memories_tenant_agent           - primary access pattern
- ix_agent_memories_tenant_agent_type      - type-filtered queries
- ix_agent_memories_tenant_agent_importance - importance-based pruning
- ix_agent_memories_tenant_agent_active    - exclude deleted
- ix_agent_memories_embedding (HNSW)       - vector similarity search
  (created only if pgvector extension is available)

Notes:
- The embedding column uses pgvector vector(1536) type.
  If pgvector is not installed the column falls back to JSONB.
- Soft-delete pattern (is_deleted) preserves audit trail.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Embedding dimension - must match the embedding model used at runtime
_EMBEDDING_DIM = 1536


def _pgvector_available(connection) -> bool:
    """Check whether pgvector extension is installed in this database."""
    result = connection.execute(
        sa.text(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
        )
    )
    return result.fetchone() is not None


def upgrade() -> None:
    """Create agent_memories table."""

    # Create the memory_type_enum type first
    memory_type_enum = postgresql.ENUM(
        "fact",
        "preference",
        "skill",
        "context",
        "episodic",
        name="memory_type_enum",
        create_type=False,
    )

    conn = op.get_bind()
    memory_type_enum.create(conn, checkfirst=True)

    # Determine whether pgvector is available
    use_vector = _pgvector_available(conn)

    # Build the embedding column type
    if use_vector:
        # Attempt to use pgvector vector type
        try:
            from pgvector.sqlalchemy import Vector
            embedding_col = sa.Column(
                "embedding",
                Vector(_EMBEDDING_DIM),
                nullable=True,
                comment=f"pgvector embedding ({_EMBEDDING_DIM}d)",
            )
        except ImportError:
            use_vector = False

    if not use_vector:
        embedding_col = sa.Column(
            "embedding",
            postgresql.JSONB,
            nullable=True,
            comment="Embedding as JSON array (pgvector unavailable)",
        )

    op.create_table(
        "agent_memories",

        # Primary key
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),

        # Tenant + agent scoping
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="Agent identity UUID (stable across conversations)",
        ),

        # Memory classification
        sa.Column(
            "memory_type",
            sa.Enum(
                "fact", "preference", "skill", "context", "episodic",
                name="memory_type_enum",
                create_constraint=False,
            ),
            nullable=False,
        ),

        # Content
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="The memory text content",
        ),

        # Embedding
        embedding_col,

        # Retention scoring
        sa.Column(
            "importance_score",
            sa.Float(),
            nullable=False,
            server_default="0.5",
            comment="Importance 0.0-1.0; decays over time",
        ),

        # Access tracking
        sa.Column(
            "access_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Number of times this memory was recalled",
        ),
        sa.Column(
            "last_accessed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp of last recall",
        ),

        # Optional TTL
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Memory expiry timestamp (NULL = never expires)",
        ),

        # Freeform metadata
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
            comment="Freeform metadata: source, confidence, tags, etc.",
        ),

        # Soft-delete
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment="Soft-delete; deleted memories excluded from recall",
        ),

        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),

        # Foreign key - tenant cascade delete
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_agent_memories_tenant_id",
        ),
    )

    # Standard indexes
    op.create_index(
        "ix_agent_memories_tenant_agent",
        "agent_memories",
        ["tenant_id", "agent_id"],
        comment="Primary access pattern: memories per agent",
    )
    op.create_index(
        "ix_agent_memories_tenant_agent_type",
        "agent_memories",
        ["tenant_id", "agent_id", "memory_type"],
        comment="Type-filtered queries",
    )
    op.create_index(
        "ix_agent_memories_tenant_agent_importance",
        "agent_memories",
        ["tenant_id", "agent_id", "importance_score"],
        comment="Importance-based pruning scans",
    )
    op.create_index(
        "ix_agent_memories_tenant_agent_active",
        "agent_memories",
        ["tenant_id", "agent_id", "is_deleted"],
        comment="Exclude soft-deleted in standard queries",
    )

    # HNSW vector index for ANN similarity search (pgvector only)
    if use_vector:
        try:
            op.execute(
                """
                CREATE INDEX ix_agent_memories_embedding
                ON agent_memories
                USING hnsw (embedding vector_cosine_ops)
                WHERE embedding IS NOT NULL AND is_deleted = false
                """
            )
        except Exception:
            # HNSW index creation is best-effort; fall back to IVFFlat or no vector index
            try:
                op.execute(
                    """
                    CREATE INDEX ix_agent_memories_embedding
                    ON agent_memories
                    USING ivfflat (embedding vector_cosine_ops)
                    WHERE embedding IS NOT NULL AND is_deleted = false
                    """
                )
            except Exception:
                pass  # Vector index is a performance optimization, not required


def downgrade() -> None:
    """Drop agent_memories table and related objects."""
    # Drop vector index if it exists
    try:
        op.drop_index("ix_agent_memories_embedding", table_name="agent_memories")
    except Exception:
        pass

    op.drop_index("ix_agent_memories_tenant_agent_active", table_name="agent_memories")
    op.drop_index("ix_agent_memories_tenant_agent_importance", table_name="agent_memories")
    op.drop_index("ix_agent_memories_tenant_agent_type", table_name="agent_memories")
    op.drop_index("ix_agent_memories_tenant_agent", table_name="agent_memories")
    op.drop_table("agent_memories")

    # Drop the enum type
    conn = op.get_bind()
    conn.execute(sa.text("DROP TYPE IF EXISTS memory_type_enum"))
