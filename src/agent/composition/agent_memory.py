"""Agent memory system for cross-agent context persistence.

Enables agents to store and retrieve context across conversations and sessions.
All memory is tenant-scoped for multi-tenancy isolation.

Memory types:
- Facts: Concrete information learned (user preferences, system state)
- Context: Recent interaction history
- Insights: Patterns and learnings from repeated interactions

Storage: PostgreSQL with tenant_id scoping, LLM-based relevance search
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient
from src.models.base import Base

log = structlog.get_logger(__name__)


@dataclass
class AgentMemory:
    """A single memory entry for an agent.

    Memories are tenant-scoped and agent-scoped for isolation.
    """

    agent_id: str
    tenant_id: uuid.UUID
    key: str
    value: str
    created_at: datetime
    access_count: int = 0
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "agent_id": self.agent_id,
            "tenant_id": str(self.tenant_id),
            "key": self.key,
            "value": self.value,
            "created_at": self.created_at.isoformat(),
            "access_count": self.access_count,
            "metadata": self.metadata or {},
        }


# SQLAlchemy model for agent_memory table
try:
    from sqlalchemy import UUID as SA_UUID
    from sqlalchemy import Column, DateTime, Index, Integer, String, Text

    class AgentMemoryModel(Base):
        """SQLAlchemy model for agent memory storage."""

        __tablename__ = "agent_memory"

        id = Column(SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
        agent_id = Column(String(128), nullable=False, index=True)
        tenant_id = Column(SA_UUID(as_uuid=True), nullable=False, index=True)
        key = Column(String(256), nullable=False)
        value = Column(Text, nullable=False)
        created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
        access_count = Column(Integer, nullable=False, default=0)
        metadata_json = Column(Text, nullable=True)

        __table_args__ = (
            Index("idx_agent_memory_lookup", "agent_id", "tenant_id", "key"),
            Index("idx_agent_memory_created", "created_at"),
        )

except ImportError:
    # Fallback if SQLAlchemy not available during imports
    AgentMemoryModel = None  # type: ignore


class AgentMemoryStore:
    """Persistent memory store for agents.

    Provides:
    - store() - Save a memory
    - retrieve() - Get a specific memory by key
    - search() - LLM-based relevance search across memories
    - cleanup() - Remove old memories (retention policy)
    - get_context_for_agent() - Get formatted context for agent prompts
    """

    def __init__(
        self,
        db: AsyncSession,
        llm_client: LLMClient,
    ) -> None:
        """Initialize agent memory store.

        Args:
            db: Database session
            llm_client: LLM client for relevance search
        """
        self._db = db
        self._llm = llm_client

    async def store(
        self,
        agent_id: str,
        tenant_id: uuid.UUID,
        key: str,
        value: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Store a memory entry.

        If a memory with this key already exists, it is updated.

        Args:
            agent_id: Agent identifier
            tenant_id: Tenant identifier (for isolation)
            key: Memory key (e.g., "user_preference_language", "last_query_topic")
            value: Memory value (string, can be JSON-serialized data)
            metadata: Optional metadata dictionary
        """
        log.debug(
            "agent_memory.store",
            agent_id=agent_id,
            tenant_id=str(tenant_id),
            key=key,
        )

        # Check if memory exists
        stmt = select(AgentMemoryModel).where(
            AgentMemoryModel.agent_id == agent_id,
            AgentMemoryModel.tenant_id == tenant_id,
            AgentMemoryModel.key == key,
        )

        result = await self._db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Update existing memory
            existing.value = value
            existing.metadata_json = json.dumps(metadata) if metadata else None
        else:
            # Create new memory
            memory = AgentMemoryModel(
                agent_id=agent_id,
                tenant_id=tenant_id,
                key=key,
                value=value,
                metadata_json=json.dumps(metadata) if metadata else None,
            )
            self._db.add(memory)

        await self._db.commit()

        log.info(
            "agent_memory.stored",
            agent_id=agent_id,
            tenant_id=str(tenant_id),
            key=key,
            action="update" if existing else "create",
        )

    async def retrieve(
        self,
        agent_id: str,
        tenant_id: uuid.UUID,
        key: str,
    ) -> AgentMemory | None:
        """Retrieve a specific memory by key.

        Args:
            agent_id: Agent identifier
            tenant_id: Tenant identifier
            key: Memory key

        Returns:
            AgentMemory if found, None otherwise
        """
        stmt = select(AgentMemoryModel).where(
            AgentMemoryModel.agent_id == agent_id,
            AgentMemoryModel.tenant_id == tenant_id,
            AgentMemoryModel.key == key,
        )

        result = await self._db.execute(stmt)
        model = result.scalar_one_or_none()

        if model is None:
            return None

        # Increment access count
        model.access_count += 1
        await self._db.commit()

        metadata = json.loads(model.metadata_json) if model.metadata_json else None

        return AgentMemory(
            agent_id=model.agent_id,
            tenant_id=model.tenant_id,
            key=model.key,
            value=model.value,
            created_at=model.created_at,
            access_count=model.access_count,
            metadata=metadata,
        )

    async def search(
        self,
        agent_id: str,
        tenant_id: uuid.UUID,
        query: str,
        limit: int = 5,
    ) -> list[AgentMemory]:
        """Search memories by relevance to query.

        Uses LLM to score relevance of each memory to the query.

        Args:
            agent_id: Agent identifier
            tenant_id: Tenant identifier
            query: Search query (natural language)
            limit: Maximum number of results

        Returns:
            List of relevant AgentMemory entries, sorted by relevance
        """
        log.debug(
            "agent_memory.search",
            agent_id=agent_id,
            tenant_id=str(tenant_id),
            query_length=len(query),
        )

        # Retrieve all memories for this agent/tenant
        stmt = select(AgentMemoryModel).where(
            AgentMemoryModel.agent_id == agent_id,
            AgentMemoryModel.tenant_id == tenant_id,
        )

        result = await self._db.execute(stmt)
        models = result.scalars().all()

        if not models:
            return []

        # Use LLM to score relevance
        memories_text = "\n\n".join(
            f"Memory {idx}: key={m.key}\nvalue={m.value}"
            for idx, m in enumerate(models)
        )

        relevance_prompt = f"""Given this query and a list of memories, score each memory's relevance from 0.0 (not relevant) to 1.0 (highly relevant).

Query: {query}

Memories:
{memories_text}

Respond in JSON format:
{{
  "scores": [
    {{"memory_index": 0, "relevance": 0.0-1.0}},
    {{"memory_index": 1, "relevance": 0.0-1.0}},
    ...
  ]
}}

Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are a relevance scoring assistant. Always respond with valid JSON only.",
            },
            {"role": "user", "content": relevance_prompt},
        ]

        try:
            response = await self._llm.complete(
                messages=messages,
                temperature=0.3,  # Low temperature for consistent scoring
                max_tokens=1024,
            )

            response_text = self._llm.extract_text(response)
            parsed = json.loads(response_text)
            scores_data = parsed.get("scores", [])

            # Build relevance map
            relevance_map: dict[int, float] = {}
            for score_entry in scores_data:
                idx = score_entry.get("memory_index")
                score = float(score_entry.get("relevance", 0.0))
                if idx is not None:
                    relevance_map[idx] = score

            # Sort memories by relevance
            scored_memories = [
                (idx, model, relevance_map.get(idx, 0.0))
                for idx, model in enumerate(models)
            ]

            scored_memories.sort(key=lambda x: x[2], reverse=True)

            # Convert top results to AgentMemory
            results: list[AgentMemory] = []
            for idx, model, score in scored_memories[:limit]:
                metadata = json.loads(model.metadata_json) if model.metadata_json else None
                metadata = metadata or {}
                metadata["relevance_score"] = score

                memory = AgentMemory(
                    agent_id=model.agent_id,
                    tenant_id=model.tenant_id,
                    key=model.key,
                    value=model.value,
                    created_at=model.created_at,
                    access_count=model.access_count,
                    metadata=metadata,
                )
                results.append(memory)

            log.info(
                "agent_memory.search_complete",
                agent_id=agent_id,
                total_memories=len(models),
                results_returned=len(results),
            )

            return results

        except json.JSONDecodeError as exc:
            log.warning("agent_memory.search_json_failed", error=str(exc))
            # Fallback: return most recently accessed memories
            sorted_models = sorted(models, key=lambda m: m.access_count, reverse=True)
            return [
                AgentMemory(
                    agent_id=m.agent_id,
                    tenant_id=m.tenant_id,
                    key=m.key,
                    value=m.value,
                    created_at=m.created_at,
                    access_count=m.access_count,
                    metadata=json.loads(m.metadata_json) if m.metadata_json else None,
                )
                for m in sorted_models[:limit]
            ]

        except Exception as exc:
            log.error("agent_memory.search_failed", error=str(exc))
            return []

    async def cleanup(
        self,
        older_than_days: int = 90,
    ) -> int:
        """Remove old memories based on retention policy.

        Args:
            older_than_days: Remove memories older than this many days

        Returns:
            Number of memories deleted
        """
        cutoff_date = datetime.now(UTC) - timedelta(days=older_than_days)

        log.info(
            "agent_memory.cleanup_start",
            older_than_days=older_than_days,
            cutoff_date=cutoff_date.isoformat(),
        )

        stmt = delete(AgentMemoryModel).where(
            AgentMemoryModel.created_at < cutoff_date
        )

        result = await self._db.execute(stmt)
        await self._db.commit()

        deleted_count = result.rowcount or 0

        log.info("agent_memory.cleanup_complete", deleted_count=deleted_count)

        return deleted_count

    async def get_context_for_agent(
        self,
        agent_id: str,
        tenant_id: uuid.UUID,
        query: str,
        max_memories: int = 3,
    ) -> str:
        """Get formatted context string for agent prompts.

        Searches relevant memories and formats them for inclusion in
        agent system prompts or messages.

        Args:
            agent_id: Agent identifier
            tenant_id: Tenant identifier
            query: Current query (for relevance search)
            max_memories: Maximum memories to include

        Returns:
            Formatted context string (empty if no relevant memories)
        """
        memories = await self.search(
            agent_id=agent_id,
            tenant_id=tenant_id,
            query=query,
            limit=max_memories,
        )

        if not memories:
            return ""

        context_lines = ["Relevant context from previous interactions:", ""]

        for memory in memories:
            context_lines.append(f"- {memory.key}: {memory.value}")

        return "\n".join(context_lines)
