"""Conversation memory extraction and retrieval for context enrichment.

Architecture:
1. Extract facts, preferences, and project context from conversation
2. Use LLM to identify extractable information
3. Store per-user memories in database table
4. Retrieve relevant memories to enrich query context

Design decisions:
- Per-user memory storage (user_id scoped)
- LLM-powered extraction for flexible pattern matching
- Memory categorization: facts, preferences, project_context, relationships
- Relevance scoring for memory retrieval
- TTL-based memory expiration (optional)
- All memories tenant-scoped

Use cases:
- "Remember that I prefer detailed technical explanations"
- "User mentioned working on Plant 3 HVAC project"
- "User's role is maintenance supervisor"

Memory structure:
- memory_id: UUID
- user_id: UUID
- tenant_id: UUID
- category: str ("fact", "preference", "project", "relationship")
- content: str (the actual memory)
- confidence: float (0.0 - 1.0)
- source_conversation_id: UUID (optional)
- created_at: datetime
- expires_at: datetime (optional)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient

log = structlog.get_logger(__name__)


@dataclass
class Memory:
    """Single memory entry."""

    memory_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    category: str
    content: str
    confidence: float
    source_conversation_id: uuid.UUID | None
    created_at: datetime
    expires_at: datetime | None
    metadata: dict[str, Any]


@dataclass
class ExtractedMemory:
    """Memory extracted from conversation before persistence."""

    category: str
    content: str
    confidence: float
    metadata: dict[str, Any]


_EXTRACTION_PROMPT_TEMPLATE = """You are a memory extraction system. Analyze the following conversation turn and extract any facts, preferences, or context that should be remembered about the user.

User message: {user_message}
Assistant response: {assistant_response}

Extract memories in the following categories:
1. FACT: Concrete information about the user (role, location, responsibilities, etc.)
2. PREFERENCE: User's stated preferences (communication style, level of detail, etc.)
3. PROJECT: Current project or task context (what they're working on)
4. RELATIONSHIP: Information about people or teams they work with

For each memory, provide:
- category: One of ["fact", "preference", "project", "relationship"]
- content: A clear, concise statement of the memory
- confidence: Float between 0.0-1.0 (how certain you are this is worth remembering)

Return a JSON array of memories. If no memories should be extracted, return an empty array.

Example output:
[
  {{"category": "fact", "content": "User is a maintenance supervisor at Plant 3", "confidence": 0.95}},
  {{"category": "preference", "content": "User prefers detailed technical explanations with code examples", "confidence": 0.85}}
]

Memories:"""


_RETRIEVAL_PROMPT_TEMPLATE = """You are a memory relevance scorer. Given a user query and a memory, score how relevant the memory is to answering the query.

Query: {query}
Memory: {memory}

Rate relevance from 0-10 where:
- 0 = completely irrelevant
- 5 = somewhat relevant, provides context
- 10 = highly relevant, essential for answering the query

Respond with ONLY a number between 0 and 10.

Score:"""


class ConversationMemoryExtractor:
    """Extract and manage conversation memories for context enrichment."""

    def __init__(
        self,
        db: AsyncSession,
        llm_client: LLMClient,
    ) -> None:
        """Initialize memory extractor.

        Args:
            db: Async database session
            llm_client: LLM client for extraction and scoring
        """
        self._db = db
        self._llm = llm_client

    async def extract_memories(
        self,
        *,
        user_message: str,
        assistant_response: str,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
    ) -> list[Memory]:
        """Extract memories from a conversation turn.

        Args:
            user_message: User's message
            assistant_response: Assistant's response
            user_id: User ID
            tenant_id: Tenant ID
            conversation_id: Optional conversation ID for tracking

        Returns:
            List of extracted and persisted Memory objects
        """
        log.debug(
            "memory.extract_start",
            user_id=str(user_id),
            tenant_id=str(tenant_id),
        )

        # Use LLM to extract memories
        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
            user_message=user_message[:2000],  # Truncate to avoid token limits
            assistant_response=assistant_response[:2000],
        )

        try:
            messages = [{"role": "user", "content": prompt}]
            response = await self._llm.complete(
                messages=messages,
                temperature=0.3,  # Slightly creative but mostly deterministic
                max_tokens=1000,
            )

            text = self._llm.extract_text(response).strip()

            # Parse JSON array
            try:
                memories_data = json.loads(text)
                if not isinstance(memories_data, list):
                    log.warning("memory.extract_invalid_format", text=text)
                    return []
            except json.JSONDecodeError as exc:
                log.warning("memory.extract_json_failed", error=str(exc), text=text)
                return []

        except Exception as exc:
            log.error("memory.extract_llm_failed", error=str(exc))
            return []

        # Persist memories
        persisted_memories: list[Memory] = []

        for mem_data in memories_data:
            try:
                category = mem_data.get("category", "fact")
                content = mem_data.get("content", "")
                confidence = float(mem_data.get("confidence", 0.5))

                if not content or confidence < 0.3:  # Skip low-confidence memories
                    continue

                memory = await self._persist_memory(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    category=category,
                    content=content,
                    confidence=confidence,
                    conversation_id=conversation_id,
                )

                persisted_memories.append(memory)

            except Exception as exc:
                log.warning("memory.persist_failed", error=str(exc), data=mem_data)
                continue

        log.info(
            "memory.extract_complete",
            user_id=str(user_id),
            tenant_id=str(tenant_id),
            count=len(persisted_memories),
        )

        return persisted_memories

    async def retrieve_relevant_memories(
        self,
        *,
        query: str,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        top_k: int = 5,
        min_relevance: float = 0.5,
    ) -> list[Memory]:
        """Retrieve memories relevant to a query.

        Args:
            query: User's query
            user_id: User ID
            tenant_id: Tenant ID
            top_k: Maximum number of memories to return
            min_relevance: Minimum relevance score (0.0 - 1.0)

        Returns:
            List of relevant Memory objects, sorted by relevance
        """
        log.debug(
            "memory.retrieve_start",
            user_id=str(user_id),
            tenant_id=str(tenant_id),
            query_preview=query[:50],
        )

        # Fetch all user memories (could be optimized with similarity search later)
        all_memories = await self._fetch_user_memories(
            user_id=user_id,
            tenant_id=tenant_id,
        )

        if not all_memories:
            return []

        # Score each memory for relevance
        scored_memories: list[tuple[Memory, float]] = []

        for memory in all_memories:
            relevance = await self._score_memory_relevance(
                query=query,
                memory_content=memory.content,
            )

            if relevance >= min_relevance:
                scored_memories.append((memory, relevance))

        # Sort by relevance (descending)
        scored_memories.sort(key=lambda x: x[1], reverse=True)

        # Return top-K
        results = [mem for mem, _ in scored_memories[:top_k]]

        log.info(
            "memory.retrieve_complete",
            user_id=str(user_id),
            tenant_id=str(tenant_id),
            total_memories=len(all_memories),
            relevant_count=len(results),
        )

        return results

    async def get_user_memories_by_category(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        category: str,
    ) -> list[Memory]:
        """Get all memories for a user filtered by category.

        Args:
            user_id: User ID
            tenant_id: Tenant ID
            category: Memory category to filter by

        Returns:
            List of Memory objects
        """
        sql = text("""
            SELECT
                id, user_id, tenant_id, category, content, confidence,
                source_conversation_id, created_at, expires_at, metadata
            FROM conversation_memories
            WHERE
                user_id = :user_id
                AND tenant_id = :tenant_id
                AND category = :category
                AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC
        """)

        result = await self._db.execute(
            sql,
            {"user_id": user_id, "tenant_id": tenant_id, "category": category},
        )
        rows = result.mappings().all()

        return [self._row_to_memory(row) for row in rows]

    async def delete_memory(
        self,
        *,
        memory_id: uuid.UUID,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> bool:
        """Delete a specific memory.

        Args:
            memory_id: Memory ID to delete
            user_id: User ID (for authorization)
            tenant_id: Tenant ID (for isolation)

        Returns:
            True if deleted, False if not found
        """
        sql = text("""
            DELETE FROM conversation_memories
            WHERE id = :memory_id
              AND user_id = :user_id
              AND tenant_id = :tenant_id
        """)

        result = await self._db.execute(
            sql,
            {"memory_id": memory_id, "user_id": user_id, "tenant_id": tenant_id},
        )

        deleted = result.rowcount > 0

        log.info(
            "memory.delete",
            memory_id=str(memory_id),
            user_id=str(user_id),
            deleted=deleted,
        )

        return deleted

    # ---- Private helpers ----

    async def _persist_memory(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        category: str,
        content: str,
        confidence: float,
        conversation_id: uuid.UUID | None = None,
        expires_in_days: int | None = None,
    ) -> Memory:
        """Persist a memory to the database.

        Args:
            user_id: User ID
            tenant_id: Tenant ID
            category: Memory category
            content: Memory content
            confidence: Confidence score
            conversation_id: Optional source conversation
            expires_in_days: Optional TTL in days

        Returns:
            Persisted Memory object
        """
        memory_id = uuid.uuid4()
        created_at = datetime.now(UTC)
        expires_at = (
            created_at + timedelta(days=expires_in_days) if expires_in_days else None
        )

        sql = text("""
            INSERT INTO conversation_memories
            (id, user_id, tenant_id, category, content, confidence,
             source_conversation_id, created_at, expires_at, metadata)
            VALUES
            (:id, :user_id, :tenant_id, :category, :content, :confidence,
             :conversation_id, :created_at, :expires_at, :metadata)
        """)

        await self._db.execute(
            sql,
            {
                "id": memory_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "category": category,
                "content": content,
                "confidence": confidence,
                "conversation_id": conversation_id,
                "created_at": created_at,
                "expires_at": expires_at,
                "metadata": json.dumps({}),
            },
        )

        await self._db.flush()

        return Memory(
            memory_id=memory_id,
            user_id=user_id,
            tenant_id=tenant_id,
            category=category,
            content=content,
            confidence=confidence,
            source_conversation_id=conversation_id,
            created_at=created_at,
            expires_at=expires_at,
            metadata={},
        )

    async def _fetch_user_memories(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> list[Memory]:
        """Fetch all non-expired memories for a user."""
        sql = text("""
            SELECT
                id, user_id, tenant_id, category, content, confidence,
                source_conversation_id, created_at, expires_at, metadata
            FROM conversation_memories
            WHERE
                user_id = :user_id
                AND tenant_id = :tenant_id
                AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC
        """)

        result = await self._db.execute(sql, {"user_id": user_id, "tenant_id": tenant_id})
        rows = result.mappings().all()

        return [self._row_to_memory(row) for row in rows]

    async def _score_memory_relevance(
        self,
        *,
        query: str,
        memory_content: str,
    ) -> float:
        """Score how relevant a memory is to a query using LLM.

        Args:
            query: User's query
            memory_content: Memory content to score

        Returns:
            Relevance score (0.0 - 1.0)
        """
        prompt = _RETRIEVAL_PROMPT_TEMPLATE.format(
            query=query[:500],
            memory=memory_content[:500],
        )

        try:
            messages = [{"role": "user", "content": prompt}]
            response = await self._llm.complete(
                messages=messages,
                temperature=0.0,  # Deterministic
                max_tokens=10,
            )

            text = self._llm.extract_text(response).strip()

            # Parse score (0-10) and normalize to 0-1
            try:
                raw_score = float(text)
                normalized = max(0.0, min(10.0, raw_score)) / 10.0
                return normalized
            except ValueError:
                return 0.5  # Default to neutral on parse failure

        except Exception as exc:
            log.warning("memory.score_failed", error=str(exc))
            return 0.5  # Default to neutral on error

    def _row_to_memory(self, row: Any) -> Memory:
        """Convert database row to Memory object."""
        return Memory(
            memory_id=row["id"],
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            category=row["category"],
            content=row["content"],
            confidence=float(row["confidence"]),
            source_conversation_id=row["source_conversation_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )


async def conversation_memory_from_context(
    db: AsyncSession,
    llm_client: LLMClient,
) -> ConversationMemoryExtractor:
    """Factory for ConversationMemoryExtractor - used by tool gateway."""
    return ConversationMemoryExtractor(db=db, llm_client=llm_client)
