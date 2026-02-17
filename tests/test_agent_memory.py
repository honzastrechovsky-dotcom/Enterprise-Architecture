"""Tests for Agent Memory & Learning system.

TDD methodology: tests define expected behavior before implementation.
Covers store, recall (semantic + by type), forget, decay, compact, stats.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def agent_id() -> uuid.UUID:
    return uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def mock_db() -> AsyncSession:
    mock = AsyncMock(spec=AsyncSession)
    mock.execute = AsyncMock()
    mock.flush = AsyncMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    mock.delete = MagicMock()
    mock.add = MagicMock()
    return mock


@pytest.fixture
def memory_service(mock_db):
    from src.services.memory import AgentMemoryService
    return AgentMemoryService(mock_db)


# ---------------------------------------------------------------------------
# Model Tests
# ---------------------------------------------------------------------------


class TestAgentMemoryModel:
    """Tests for the AgentMemory ORM model."""

    def test_memory_type_enum_has_required_values(self):
        """MemoryType enum must have FACT, PREFERENCE, SKILL, CONTEXT, EPISODIC."""
        from src.models.agent_memory import MemoryType

        assert MemoryType.FACT == "fact"
        assert MemoryType.PREFERENCE == "preference"
        assert MemoryType.SKILL == "skill"
        assert MemoryType.CONTEXT == "context"
        assert MemoryType.EPISODIC == "episodic"

    def test_agent_memory_model_has_required_fields(self):
        """AgentMemory model must have all required fields."""
        from src.models.agent_memory import AgentMemory

        # Verify model class has expected column attributes
        assert hasattr(AgentMemory, "id")
        assert hasattr(AgentMemory, "tenant_id")
        assert hasattr(AgentMemory, "agent_id")
        assert hasattr(AgentMemory, "memory_type")
        assert hasattr(AgentMemory, "content")
        assert hasattr(AgentMemory, "importance_score")
        assert hasattr(AgentMemory, "access_count")
        assert hasattr(AgentMemory, "last_accessed_at")
        assert hasattr(AgentMemory, "expires_at")
        assert hasattr(AgentMemory, "metadata_")
        assert hasattr(AgentMemory, "is_deleted")
        assert hasattr(AgentMemory, "created_at")
        assert hasattr(AgentMemory, "updated_at")

    def test_agent_memory_instantiation(self, tenant_id, agent_id):
        """AgentMemory can be instantiated with all required fields explicitly set."""
        from src.models.agent_memory import AgentMemory, MemoryType

        mem = AgentMemory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.FACT,
            content="Python is a programming language",
            importance_score=0.5,
            access_count=0,
            is_deleted=False,
        )

        assert mem.tenant_id == tenant_id
        assert mem.agent_id == agent_id
        assert mem.memory_type == MemoryType.FACT
        assert mem.content == "Python is a programming language"
        assert mem.importance_score == 0.5
        assert mem.access_count == 0
        assert mem.is_deleted is False


# ---------------------------------------------------------------------------
# Service Tests - store_memory
# ---------------------------------------------------------------------------


class TestStoreMemory:
    """Tests for AgentMemoryService.store_memory."""

    @pytest.mark.asyncio
    async def test_store_memory_returns_agent_memory(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """store_memory returns an AgentMemory instance."""
        from src.models.agent_memory import AgentMemory, MemoryType

        result = await memory_service.store_memory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.FACT,
            content="The user prefers concise answers",
        )

        assert isinstance(result, AgentMemory)
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_memory_with_custom_importance(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """store_memory respects custom importance_score."""
        from src.models.agent_memory import AgentMemory, MemoryType

        result = await memory_service.store_memory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.PREFERENCE,
            content="User prefers dark mode",
            importance=0.9,
        )

        assert result.importance_score == 0.9

    @pytest.mark.asyncio
    async def test_store_memory_with_metadata(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """store_memory stores metadata dict."""
        from src.models.agent_memory import AgentMemory, MemoryType

        meta = {"source": "conversation", "confidence": 0.85}
        result = await memory_service.store_memory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.SKILL,
            content="Can write Python code",
            metadata=meta,
        )

        assert result.metadata_ == meta

    @pytest.mark.asyncio
    async def test_store_memory_with_expiry(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """store_memory stores optional expires_at."""
        from src.models.agent_memory import AgentMemory, MemoryType

        expires = datetime.now(timezone.utc) + timedelta(days=30)
        result = await memory_service.store_memory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.CONTEXT,
            content="Current project: Phoenix rebuild",
            expires_at=expires,
        )

        assert result.expires_at == expires


# ---------------------------------------------------------------------------
# Service Tests - recall_by_type
# ---------------------------------------------------------------------------


class TestRecallByType:
    """Tests for AgentMemoryService.recall_by_type."""

    @pytest.mark.asyncio
    async def test_recall_by_type_returns_list(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """recall_by_type returns a list of AgentMemory objects."""
        from src.models.agent_memory import AgentMemory, MemoryType

        mem1 = AgentMemory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.FACT,
            content="Fact 1",
        )
        mem2 = AgentMemory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.FACT,
            content="Fact 2",
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mem1, mem2]
        mock_db.execute.return_value = mock_result

        results = await memory_service.recall_by_type(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.FACT,
        )

        assert len(results) == 2
        assert all(isinstance(r, AgentMemory) for r in results)

    @pytest.mark.asyncio
    async def test_recall_by_type_respects_limit(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """recall_by_type respects limit parameter."""
        from src.models.agent_memory import MemoryType

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        await memory_service.recall_by_type(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.PREFERENCE,
            limit=5,
        )

        # execute was called (query was constructed and run)
        mock_db.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Service Tests - forget
# ---------------------------------------------------------------------------


class TestForgetMemory:
    """Tests for AgentMemoryService.forget."""

    @pytest.mark.asyncio
    async def test_forget_soft_deletes_memory(
        self, memory_service, mock_db, tenant_id
    ):
        """forget soft-deletes memory by setting is_deleted=True."""
        from src.models.agent_memory import AgentMemory, MemoryType

        memory_id = uuid.uuid4()
        mem = AgentMemory(
            id=memory_id,
            tenant_id=tenant_id,
            agent_id=uuid.uuid4(),
            memory_type=MemoryType.FACT,
            content="To be forgotten",
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mem
        mock_db.execute.return_value = mock_result

        result = await memory_service.forget(
            memory_id=memory_id,
            tenant_id=tenant_id,
        )

        assert result is True
        assert mem.is_deleted is True
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_forget_returns_false_when_not_found(
        self, memory_service, mock_db, tenant_id
    ):
        """forget returns False when memory not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await memory_service.forget(
            memory_id=uuid.uuid4(),
            tenant_id=tenant_id,
        )

        assert result is False


# ---------------------------------------------------------------------------
# Service Tests - decay_memories
# ---------------------------------------------------------------------------


class TestDecayMemories:
    """Tests for AgentMemoryService.decay_memories."""

    @pytest.mark.asyncio
    async def test_decay_reduces_importance_of_old_memories(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """decay_memories reduces importance scores."""
        from src.models.agent_memory import AgentMemory, MemoryType

        old_mem = AgentMemory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.CONTEXT,
            content="Old context",
            importance_score=0.8,
        )
        old_mem.last_accessed_at = datetime.now(timezone.utc) - timedelta(days=30)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [old_mem]
        mock_db.execute.return_value = mock_result

        decayed_count = await memory_service.decay_memories(
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

        assert isinstance(decayed_count, int)
        assert decayed_count >= 0
        # importance should have decreased
        assert old_mem.importance_score < 0.8

    @pytest.mark.asyncio
    async def test_decay_does_not_go_below_zero(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """decay_memories never sets importance below 0."""
        from src.models.agent_memory import AgentMemory, MemoryType

        mem = AgentMemory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.CONTEXT,
            content="Nearly forgotten",
            importance_score=0.01,
        )
        mem.last_accessed_at = datetime.now(timezone.utc) - timedelta(days=90)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mem]
        mock_db.execute.return_value = mock_result

        await memory_service.decay_memories(
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

        assert mem.importance_score >= 0.0


# ---------------------------------------------------------------------------
# Service Tests - compact_memories
# ---------------------------------------------------------------------------


class TestCompactMemories:
    """Tests for AgentMemoryService.compact_memories."""

    @pytest.mark.asyncio
    async def test_compact_returns_stats_dict(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """compact_memories returns a dict with compaction stats."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar.return_value = 0
        mock_db.execute.return_value = mock_result

        result = await memory_service.compact_memories(
            tenant_id=tenant_id,
            agent_id=agent_id,
            max_memories=1000,
        )

        assert isinstance(result, dict)
        assert "deleted" in result
        assert "remaining" in result

    @pytest.mark.asyncio
    async def test_compact_soft_deletes_low_importance_when_over_limit(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """compact_memories soft-deletes oldest low-importance memories when over limit."""
        from src.models.agent_memory import AgentMemory, MemoryType

        # Create 5 memories with low importance
        memories = []
        for i in range(5):
            mem = AgentMemory(
                tenant_id=tenant_id,
                agent_id=agent_id,
                memory_type=MemoryType.EPISODIC,
                content=f"Old episodic memory {i}",
                importance_score=0.1,
            )
            memories.append(mem)

        # Mock count query returns 105 (over limit of 100)
        count_result = MagicMock()
        count_result.scalar.return_value = 105

        # Mock list query returns the 5 low-importance memories
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = memories

        mock_db.execute.side_effect = [count_result, list_result]

        result = await memory_service.compact_memories(
            tenant_id=tenant_id,
            agent_id=agent_id,
            max_memories=100,
        )

        # All 5 should have been soft-deleted
        assert result["deleted"] == 5
        for mem in memories:
            assert mem.is_deleted is True


# ---------------------------------------------------------------------------
# Service Tests - get_memory_stats
# ---------------------------------------------------------------------------


class TestGetMemoryStats:
    """Tests for AgentMemoryService.get_memory_stats."""

    @pytest.mark.asyncio
    async def test_get_memory_stats_returns_dict(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """get_memory_stats returns a stats dict with expected keys."""
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (10, 5, 3, 2, 0, 0.65)
        mock_result.mappings.return_value.all.return_value = [
            {"memory_type": "fact", "count": 5},
            {"memory_type": "preference", "count": 3},
        ]
        mock_db.execute.return_value = mock_result

        stats = await memory_service.get_memory_stats(
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

        assert isinstance(stats, dict)
        assert "total" in stats
        assert "by_type" in stats
        assert "avg_importance" in stats

    @pytest.mark.asyncio
    async def test_get_memory_stats_correct_structure(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """get_memory_stats dict has correct nested structure."""
        from src.models.agent_memory import AgentMemory, MemoryType

        # Simulate DB returning count=3, avg_importance=0.7
        count_result = MagicMock()
        count_result.scalar.return_value = 3

        avg_result = MagicMock()
        avg_result.scalar.return_value = 0.7

        by_type_result = MagicMock()
        by_type_result.all.return_value = [
            (MemoryType.FACT, 2),
            (MemoryType.PREFERENCE, 1),
        ]

        mock_db.execute.side_effect = [count_result, avg_result, by_type_result]

        stats = await memory_service.get_memory_stats(
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

        assert isinstance(stats["total"], int)
        assert isinstance(stats["avg_importance"], float)
        assert isinstance(stats["by_type"], dict)


# ---------------------------------------------------------------------------
# Service Tests - recall_memories (semantic search)
# ---------------------------------------------------------------------------


class TestRecallMemories:
    """Tests for AgentMemoryService.recall_memories (semantic search)."""

    @pytest.mark.asyncio
    async def test_recall_memories_without_embeddings_falls_back_to_text_search(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """recall_memories falls back to content search when no embeddings."""
        from src.models.agent_memory import AgentMemory, MemoryType

        mem = AgentMemory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.FACT,
            content="Python is used for data science",
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mem]
        mock_db.execute.return_value = mock_result

        results = await memory_service.recall_memories(
            tenant_id=tenant_id,
            agent_id=agent_id,
            query="data science tools",
            limit=5,
        )

        assert isinstance(results, list)
        mock_db.execute.assert_called()

    @pytest.mark.asyncio
    async def test_recall_memories_updates_access_tracking(
        self, memory_service, mock_db, tenant_id, agent_id
    ):
        """recall_memories increments access_count and updates last_accessed_at."""
        from src.models.agent_memory import AgentMemory, MemoryType

        mem = AgentMemory(
            tenant_id=tenant_id,
            agent_id=agent_id,
            memory_type=MemoryType.FACT,
            content="Some fact",
            access_count=5,
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mem]
        mock_db.execute.return_value = mock_result

        await memory_service.recall_memories(
            tenant_id=tenant_id,
            agent_id=agent_id,
            query="some fact",
        )

        assert mem.access_count == 6
        assert mem.last_accessed_at is not None
