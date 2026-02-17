"""Agent Memory model for persistent agent learning and recall.

AgentMemory stores typed memories per agent (scoped to tenant + agent_id).
Memory types cover facts, preferences, skills, context, and episodic memories.

Embeddings are stored when pgvector is available, enabling semantic similarity
search. Without pgvector, recall falls back to full-text/ilike search.

Memory lifecycle:
- Memories can be soft-deleted (is_deleted=True) via forget()
- Importance decays over time via decay_memories()
- Old low-importance memories are pruned via compact_memories()
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

# pgvector is optional - import conditionally
try:
    from pgvector.sqlalchemy import Vector as PgVector
    _PGVECTOR_AVAILABLE = True
except ImportError:
    _PGVECTOR_AVAILABLE = False


class MemoryType(StrEnum):
    """Taxonomy of agent memory types."""
    FACT = "fact"                  # Factual knowledge about the world or user
    PREFERENCE = "preference"      # User preferences and style choices
    SKILL = "skill"                # Learned agent capabilities or procedures
    CONTEXT = "context"            # Situational context for current tasks
    EPISODIC = "episodic"          # Past interaction episodes and outcomes


# Embedding dimension must match the embedding model output.
# 1536 matches text-embedding-3-small / text-embedding-ada-002.
_EMBEDDING_DIM = 1536


class AgentMemory(Base):
    """Persistent memory entry scoped to a tenant + agent pair.

    Each record captures one piece of memory with:
    - Type classification (MemoryType enum)
    - Content (the actual memory text)
    - Optional embedding for semantic similarity search
    - Importance score (0.0 - 1.0) for retention decisions
    - Access tracking for LRU-style decay
    - Soft-delete support via is_deleted flag
    """

    __tablename__ = "agent_memories"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    # Multi-tenancy scoping - MANDATORY
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Agent scoping - memory belongs to a specific agent identity
    agent_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True,
        comment="Agent identity this memory belongs to",
    )

    # Memory classification
    memory_type: Mapped[MemoryType] = mapped_column(
        Enum(MemoryType, name="memory_type_enum"),
        nullable=False,
        index=True,
    )

    # The memory content
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The memory text content",
    )

    # Semantic embedding vector (NULL when pgvector unavailable or not yet embedded)
    # Defined as generic column to avoid hard dependency on pgvector at import time
    if _PGVECTOR_AVAILABLE:
        embedding: Mapped[Any] = mapped_column(
            PgVector(_EMBEDDING_DIM),
            nullable=True,
            comment=f"Vector embedding ({_EMBEDDING_DIM}d) for semantic search",
        )
    else:
        embedding: Mapped[Any] = mapped_column(
            JSONB,
            nullable=True,
            comment="Embedding stored as JSON array (pgvector unavailable)",
        )

    # Retention metadata
    importance_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.5,
        server_default="0.5",
        comment="Importance score 0.0-1.0; decays over time",
    )

    access_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Number of times this memory was recalled",
    )

    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of last recall",
    )

    # Optional TTL - NULL means memory never expires
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this memory expires (NULL = never)",
    )

    # Arbitrary key-value metadata
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Freeform metadata: source, confidence, tags, etc.",
    )

    # Soft-delete instead of hard-delete to preserve audit trail
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Soft-delete flag; deleted memories are excluded from queries",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        # Primary access pattern: fetch all memories for an agent
        Index(
            "ix_agent_memories_tenant_agent",
            "tenant_id",
            "agent_id",
        ),
        # Filter by type within an agent
        Index(
            "ix_agent_memories_tenant_agent_type",
            "tenant_id",
            "agent_id",
            "memory_type",
        ),
        # Importance-based pruning scans
        Index(
            "ix_agent_memories_tenant_agent_importance",
            "tenant_id",
            "agent_id",
            "importance_score",
        ),
        # Exclude soft-deleted from standard queries
        Index(
            "ix_agent_memories_tenant_agent_active",
            "tenant_id",
            "agent_id",
            "is_deleted",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentMemory id={self.id} agent={self.agent_id} "
            f"type={self.memory_type} importance={self.importance_score:.2f}>"
        )
