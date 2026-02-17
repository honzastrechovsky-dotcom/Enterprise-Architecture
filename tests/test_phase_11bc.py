"""Tests for Phase 11B and 11C: Memory injection, learning loop, feedback memories,
and feedback-weighted RAG retrieval.

Covers:
- 11B2: _extract_and_store_preferences (fire-and-forget preference extraction)
- 11B3: _select_agent_memory_aware (memory-guided specialist selection)
- 11C1: FeedbackService._store_feedback_memories (feedback -> memory)
- 11C2: _learn_from_interaction (LEARN step)
- 11C3: RetrievalService._apply_feedback_weights (feedback-weighted RAG)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
def agent_id() -> uuid.UUID:
    return uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture
def mock_db() -> AsyncSession:
    mock = AsyncMock(spec=AsyncSession)
    mock.execute = AsyncMock()
    mock.flush = AsyncMock()
    mock.add = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# 11B2: _extract_and_store_preferences
# ---------------------------------------------------------------------------


class TestExtractAndStorePreferences:
    """Tests for the fire-and-forget preference extraction background task."""

    @pytest.mark.asyncio
    async def test_stores_preference_memory_on_preference_line(
        self, mock_db, tenant_id, agent_id
    ):
        """Extracts PREFERENCE line from LLM and stores as PREFERENCE memory."""
        from src.agent.runtime import _extract_and_store_preferences
        from src.models.agent_memory import MemoryType

        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(return_value="PREFERENCE|User prefers concise answers")
        mock_llm.extract_text = MagicMock(return_value="PREFERENCE|User prefers concise answers")

        stored_memories = []

        async def fake_store(**kwargs):
            stored_memories.append(kwargs)
            mem = MagicMock()
            return mem

        with patch("src.agent.runtime.AgentMemoryService") as MockService:
            instance = MockService.return_value
            instance.store_memory = AsyncMock(side_effect=fake_store)

            await _extract_and_store_preferences(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_message="Give me a brief summary",
                agent_response="Here is a detailed and lengthy answer...",
                llm_client=mock_llm,
                db=mock_db,
            )

        assert len(stored_memories) == 1
        assert stored_memories[0]["memory_type"] == MemoryType.PREFERENCE
        assert "concise" in stored_memories[0]["content"]

    @pytest.mark.asyncio
    async def test_stores_fact_memory_on_fact_line(
        self, mock_db, tenant_id, agent_id
    ):
        """Extracts FACT line from LLM and stores as FACT memory."""
        from src.agent.runtime import _extract_and_store_preferences
        from src.models.agent_memory import MemoryType

        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(return_value="FACT|Machine X model is ABC-123")
        mock_llm.extract_text = MagicMock(return_value="FACT|Machine X model is ABC-123")

        stored_memories = []

        async def fake_store(**kwargs):
            stored_memories.append(kwargs)
            return MagicMock()

        with patch("src.agent.runtime.AgentMemoryService") as MockService:
            instance = MockService.return_value
            instance.store_memory = AsyncMock(side_effect=fake_store)

            await _extract_and_store_preferences(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_message="What is the model number for machine X?",
                agent_response="Machine X model is ABC-123",
                llm_client=mock_llm,
                db=mock_db,
            )

        assert len(stored_memories) == 1
        assert stored_memories[0]["memory_type"] == MemoryType.FACT
        assert "ABC-123" in stored_memories[0]["content"]

    @pytest.mark.asyncio
    async def test_skips_storage_when_llm_returns_none(
        self, mock_db, tenant_id, agent_id
    ):
        """Skips memory storage when LLM responds NONE."""
        from src.agent.runtime import _extract_and_store_preferences

        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(return_value="NONE")
        mock_llm.extract_text = MagicMock(return_value="NONE")

        with patch("src.agent.runtime.AgentMemoryService") as MockService:
            instance = MockService.return_value
            instance.store_memory = AsyncMock()

            await _extract_and_store_preferences(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_message="Hello",
                agent_response="Hi there!",
                llm_client=mock_llm,
                db=mock_db,
            )

            instance.store_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_fails_open_on_llm_exception(
        self, mock_db, tenant_id, agent_id
    ):
        """Does not raise if LLM call fails - graceful degradation."""
        from src.agent.runtime import _extract_and_store_preferences

        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        # Should not raise
        await _extract_and_store_preferences(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_message="Test",
            agent_response="Test response",
            llm_client=mock_llm,
            db=mock_db,
        )

    @pytest.mark.asyncio
    async def test_stores_multiple_items_from_multiline_response(
        self, mock_db, tenant_id, agent_id
    ):
        """Stores multiple memories when LLM returns multiple TYPE|content lines."""
        from src.agent.runtime import _extract_and_store_preferences
        from src.models.agent_memory import MemoryType

        multi_line = "PREFERENCE|User wants bullet points\nFACT|Line 3 is the critical line"
        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(return_value=multi_line)
        mock_llm.extract_text = MagicMock(return_value=multi_line)

        stored_memories = []

        async def fake_store(**kwargs):
            stored_memories.append(kwargs)
            return MagicMock()

        with patch("src.agent.runtime.AgentMemoryService") as MockService:
            instance = MockService.return_value
            instance.store_memory = AsyncMock(side_effect=fake_store)

            await _extract_and_store_preferences(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_message="Check Line 3 first always",
                agent_response="I will always check Line 3 first and use bullet points",
                llm_client=mock_llm,
                db=mock_db,
            )

        assert len(stored_memories) == 2
        types = {m["memory_type"] for m in stored_memories}
        assert MemoryType.PREFERENCE in types
        assert MemoryType.FACT in types


# ---------------------------------------------------------------------------
# 11B3: _select_agent_memory_aware
# ---------------------------------------------------------------------------


class TestSelectAgentMemoryAware:
    """Tests for memory-guided specialist selection in orchestrator."""

    @pytest.fixture
    def mock_orchestrator(self, mock_db):
        """Create orchestrator with mocked dependencies."""
        from src.agent.orchestrator import AgentOrchestrator

        settings = MagicMock()
        settings.litellm_default_model = "openai/gpt-4o"
        llm_client = MagicMock()
        tool_gateway = MagicMock()

        with (
            patch("src.agent.orchestrator.get_registry") as mock_registry,
            patch("src.agent.orchestrator.ClassificationPolicy"),
            patch("src.agent.orchestrator.DisclosureService"),
            patch("src.agent.orchestrator.get_skill_registry"),
            patch("src.agent.orchestrator.RedTeam"),
        ):
            mock_registry.return_value = MagicMock()
            orch = AgentOrchestrator(
                db=mock_db,
                settings=settings,
                llm_client=llm_client,
                tool_gateway=tool_gateway,
            )
        return orch

    @pytest.mark.asyncio
    async def test_high_confidence_skips_memory_lookup(
        self, mock_orchestrator, mock_db, tenant_id
    ):
        """High-confidence intents bypass memory lookup entirely."""
        from src.agent.orchestrator import IntentClassification
        from src.models.user import UserRole

        intent = IntentClassification(
            primary_capability="general_knowledge",
            confidence=0.95,
        )

        expected_spec = MagicMock()
        mock_orchestrator._select_agent = MagicMock(return_value=expected_spec)

        with patch("src.agent.orchestrator.AgentMemoryService") as MockMemSvc:
            result = await mock_orchestrator._select_agent_memory_aware(
                intent=intent,
                user_role=UserRole.VIEWER,
                tenant_id=tenant_id,
            )

            # Memory service should not be called for high-confidence
            MockMemSvc.assert_not_called()

        assert result is expected_spec

    @pytest.mark.asyncio
    async def test_low_confidence_queries_skill_memories(
        self, mock_orchestrator, mock_db, tenant_id
    ):
        """Low-confidence intents trigger SKILL memory lookup."""
        from src.agent.orchestrator import IntentClassification
        from src.models.user import UserRole

        intent = IntentClassification(
            primary_capability="quality_check",
            confidence=0.45,
        )

        default_spec = MagicMock()
        mock_orchestrator._select_agent = MagicMock(return_value=default_spec)

        with patch("src.agent.orchestrator.AgentMemoryService") as MockMemSvc:
            instance = MockMemSvc.return_value
            instance.recall_by_type = AsyncMock(return_value=[])

            result = await mock_orchestrator._select_agent_memory_aware(
                intent=intent,
                user_role=UserRole.VIEWER,
                tenant_id=tenant_id,
            )

            instance.recall_by_type.assert_called_once()

        assert result is default_spec

    @pytest.mark.asyncio
    async def test_uses_memory_preferred_agent_for_low_confidence(
        self, mock_orchestrator, mock_db, tenant_id
    ):
        """Prefers memory-guided agent when SKILL memory matches capability."""
        from src.agent.orchestrator import IntentClassification
        from src.models.agent_memory import AgentMemory, MemoryType
        from src.models.user import UserRole

        intent = IntentClassification(
            primary_capability="quality_check",
            confidence=0.50,
        )

        # Create a SKILL memory pointing to quality_inspector (must be valid UUID)
        quality_uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        skill_mem = MagicMock(spec=AgentMemory)
        skill_mem.content = f"agent_id:{quality_uuid} worked well for quality_check"
        skill_mem.memory_type = MemoryType.SKILL

        # The preferred agent spec
        preferred_spec = MagicMock()
        preferred_spec.agent_id = quality_uuid
        preferred_spec.required_role = UserRole.VIEWER

        default_spec = MagicMock()
        mock_orchestrator._select_agent = MagicMock(return_value=default_spec)

        # Registry lists this agent
        mock_orchestrator._registry.list_agents.return_value = [preferred_spec]

        with patch("src.agent.orchestrator.AgentMemoryService") as MockMemSvc:
            instance = MockMemSvc.return_value
            instance.recall_by_type = AsyncMock(return_value=[skill_mem])

            result = await mock_orchestrator._select_agent_memory_aware(
                intent=intent,
                user_role=UserRole.VIEWER,
                tenant_id=tenant_id,
            )

        assert result is preferred_spec

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_memory_lookup_fails(
        self, mock_orchestrator, mock_db, tenant_id
    ):
        """Falls back to standard selection if memory lookup raises an exception."""
        from src.agent.orchestrator import IntentClassification
        from src.models.user import UserRole

        intent = IntentClassification(
            primary_capability="quality_check",
            confidence=0.40,
        )

        default_spec = MagicMock()
        mock_orchestrator._select_agent = MagicMock(return_value=default_spec)

        with patch("src.agent.orchestrator.AgentMemoryService") as MockMemSvc:
            instance = MockMemSvc.return_value
            instance.recall_by_type = AsyncMock(side_effect=RuntimeError("DB error"))

            result = await mock_orchestrator._select_agent_memory_aware(
                intent=intent,
                user_role=UserRole.VIEWER,
                tenant_id=tenant_id,
            )

        assert result is default_spec


# ---------------------------------------------------------------------------
# 11C1: FeedbackService._store_feedback_memories
# ---------------------------------------------------------------------------


class TestFeedbackMemories:
    """Tests for memory storage triggered by feedback submission."""

    def _make_feedback(self, tenant_id, rating: str, comment: str | None = None):
        """Build a mock ResponseFeedback-like object."""
        from src.models.feedback import FeedbackRating, ResponseFeedback

        fb = MagicMock(spec=ResponseFeedback)
        fb.id = uuid.uuid4()
        fb.tenant_id = tenant_id
        fb.user_id = uuid.uuid4()
        fb.rating = FeedbackRating(rating)
        fb.comment = comment
        fb.prompt_text = "Test prompt for quality assessment"
        fb.response_text = "Test response"
        fb.model_used = "openai/gpt-4o"
        return fb

    @pytest.mark.asyncio
    async def test_negative_thumbsdown_stores_fact_memory(
        self, mock_db, tenant_id
    ):
        """Thumbs-down feedback stores a FACT memory about the failure."""
        from src.models.agent_memory import MemoryType
        from src.services.feedback import FeedbackService

        svc = FeedbackService(mock_db)
        feedback = self._make_feedback(tenant_id, "thumbs_down", comment="Too verbose")

        stored = []

        async def fake_store(**kwargs):
            stored.append(kwargs)
            return MagicMock()

        with patch("src.services.feedback.AgentMemoryService") as MockSvc:
            instance = MockSvc.return_value
            instance.store_memory = AsyncMock(side_effect=fake_store)

            await svc._store_feedback_memories(
                feedback=feedback,
                agent_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            )

        types = [m["memory_type"] for m in stored]
        assert MemoryType.FACT in types

    @pytest.mark.asyncio
    async def test_negative_with_comment_stores_preference_memory(
        self, mock_db, tenant_id
    ):
        """Negative feedback with a comment also stores a PREFERENCE memory."""
        from src.models.agent_memory import MemoryType
        from src.services.feedback import FeedbackService

        svc = FeedbackService(mock_db)
        feedback = self._make_feedback(
            tenant_id, "rating_2", comment="Please be more concise"
        )

        stored = []

        async def fake_store(**kwargs):
            stored.append(kwargs)
            return MagicMock()

        with patch("src.services.feedback.AgentMemoryService") as MockSvc:
            instance = MockSvc.return_value
            instance.store_memory = AsyncMock(side_effect=fake_store)

            await svc._store_feedback_memories(
                feedback=feedback,
                agent_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            )

        types = [m["memory_type"] for m in stored]
        assert MemoryType.FACT in types
        assert MemoryType.PREFERENCE in types

    @pytest.mark.asyncio
    async def test_positive_feedback_stores_skill_memory(
        self, mock_db, tenant_id
    ):
        """Positive feedback stores a SKILL memory about what worked."""
        from src.models.agent_memory import MemoryType
        from src.services.feedback import FeedbackService

        svc = FeedbackService(mock_db)
        feedback = self._make_feedback(tenant_id, "thumbs_up")

        stored = []

        async def fake_store(**kwargs):
            stored.append(kwargs)
            return MagicMock()

        with patch("src.services.feedback.AgentMemoryService") as MockSvc:
            instance = MockSvc.return_value
            instance.store_memory = AsyncMock(side_effect=fake_store)

            await svc._store_feedback_memories(
                feedback=feedback,
                agent_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            )

        types = [m["memory_type"] for m in stored]
        assert MemoryType.SKILL in types
        assert len(stored) == 1  # Only skill memory for positive

    @pytest.mark.asyncio
    async def test_rating_3_stores_no_memories(
        self, mock_db, tenant_id
    ):
        """Neutral rating (3) does not trigger any memory storage."""
        from src.services.feedback import FeedbackService

        svc = FeedbackService(mock_db)
        feedback = self._make_feedback(tenant_id, "rating_3")

        with patch("src.services.feedback.AgentMemoryService") as MockSvc:
            instance = MockSvc.return_value
            instance.store_memory = AsyncMock()

            await svc._store_feedback_memories(
                feedback=feedback,
                agent_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            )

            instance.store_memory.assert_not_called()


# ---------------------------------------------------------------------------
# 11C2: _learn_from_interaction
# ---------------------------------------------------------------------------


class TestLearnFromInteraction:
    """Tests for the rule-based LEARN step background task."""

    @pytest.mark.asyncio
    async def test_stores_episodic_memory_with_citations(
        self, mock_db, tenant_id, agent_id
    ):
        """LEARN step stores EPISODIC memory noting citations were used."""
        from src.agent.runtime import _learn_from_interaction
        from src.models.agent_memory import MemoryType

        stored = []

        async def fake_store(**kwargs):
            stored.append(kwargs)
            return MagicMock()

        with patch("src.agent.runtime.AgentMemoryService") as MockSvc:
            instance = MockSvc.return_value
            instance.store_memory = AsyncMock(side_effect=fake_store)

            await _learn_from_interaction(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_message="What is the maintenance schedule?",
                agent_response="According to doc X the schedule is...",
                has_citations=True,
                db=mock_db,
            )

        assert len(stored) == 1
        assert stored[0]["memory_type"] == MemoryType.EPISODIC
        assert "with citations" in stored[0]["content"]

    @pytest.mark.asyncio
    async def test_stores_episodic_memory_without_citations(
        self, mock_db, tenant_id, agent_id
    ):
        """LEARN step notes when no citations were used."""
        from src.agent.runtime import _learn_from_interaction
        from src.models.agent_memory import MemoryType

        stored = []

        async def fake_store(**kwargs):
            stored.append(kwargs)
            return MagicMock()

        with patch("src.agent.runtime.AgentMemoryService") as MockSvc:
            instance = MockSvc.return_value
            instance.store_memory = AsyncMock(side_effect=fake_store)

            await _learn_from_interaction(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_message="What is 2+2?",
                agent_response="4",
                has_citations=False,
                db=mock_db,
            )

        assert len(stored) == 1
        assert "without citations" in stored[0]["content"]

    @pytest.mark.asyncio
    async def test_learn_step_fails_open_on_exception(
        self, mock_db, tenant_id, agent_id
    ):
        """LEARN step does not raise on DB failure - graceful degradation."""
        from src.agent.runtime import _learn_from_interaction

        with patch("src.agent.runtime.AgentMemoryService") as MockSvc:
            instance = MockSvc.return_value
            instance.store_memory = AsyncMock(side_effect=RuntimeError("DB down"))

            # Must not raise
            await _learn_from_interaction(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_message="Hello",
                agent_response="Hi",
                has_citations=False,
                db=mock_db,
            )

    @pytest.mark.asyncio
    async def test_learn_step_metadata_has_has_citations_flag(
        self, mock_db, tenant_id, agent_id
    ):
        """LEARN step metadata records the has_citations boolean."""
        from src.agent.runtime import _learn_from_interaction

        stored = []

        async def fake_store(**kwargs):
            stored.append(kwargs)
            return MagicMock()

        with patch("src.agent.runtime.AgentMemoryService") as MockSvc:
            instance = MockSvc.return_value
            instance.store_memory = AsyncMock(side_effect=fake_store)

            await _learn_from_interaction(
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_message="Test",
                agent_response="Test answer",
                has_citations=True,
                db=mock_db,
            )

        assert stored[0]["metadata"]["has_citations"] is True


# ---------------------------------------------------------------------------
# 11C3: RetrievalService._apply_feedback_weights
# ---------------------------------------------------------------------------


class TestApplyFeedbackWeights:
    """Tests for feedback-weighted score adjustment in RAG retrieval."""

    @pytest.fixture
    def retrieval_service(self, mock_db):
        from src.rag.retrieve import RetrievalService

        settings = MagicMock()
        settings.vector_top_k = 5
        llm_client = MagicMock()

        with (
            patch("src.rag.retrieve.HybridSearchEngine"),
            patch("src.rag.retrieve.CrossEncoderReranker"),
        ):
            svc = RetrievalService(
                db=mock_db,
                settings=settings,
                llm_client=llm_client,
            )
        return svc

    @pytest.mark.asyncio
    async def test_returns_unchanged_when_no_chunks(
        self, retrieval_service, tenant_id
    ):
        """Empty chunk list returns empty list unchanged."""
        result = await retrieval_service._apply_feedback_weights(
            chunks=[], tenant_id=tenant_id
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_unchanged_when_no_sentiment_data(
        self, retrieval_service, mock_db, tenant_id
    ):
        """Chunks returned unchanged when feedback query finds no sentiment."""
        chunks = [
            {
                "chunk_id": "c1",
                "document_id": "doc-111",
                "similarity_score": 0.80,
                "document_name": "doc1",
                "document_version": 1,
                "chunk_index": 0,
                "content": "content",
                "metadata": {},
            }
        ]

        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        result = await retrieval_service._apply_feedback_weights(
            chunks=chunks, tenant_id=tenant_id
        )

        assert result[0]["similarity_score"] == pytest.approx(0.80)

    @pytest.mark.asyncio
    async def test_feedback_weighting_skipped_returns_unchanged(
        self, retrieval_service, mock_db, tenant_id
    ):
        """Feedback weighting is skipped (schema lacks document_id) — scores unchanged."""
        chunks = [
            {
                "chunk_id": "c1",
                "document_id": "doc-positive",
                "similarity_score": 0.80,
                "document_name": "doc1",
                "document_version": 1,
                "chunk_index": 0,
                "content": "content",
                "metadata": {},
            },
            {
                "chunk_id": "c2",
                "document_id": "doc-negative",
                "similarity_score": 0.60,
                "document_name": "doc2",
                "document_version": 1,
                "chunk_index": 0,
                "content": "other content",
                "metadata": {},
            },
        ]

        result = await retrieval_service._apply_feedback_weights(
            chunks=chunks, tenant_id=tenant_id
        )

        # Scores unchanged — feedback weighting disabled until schema supports doc-level feedback
        assert result[0]["similarity_score"] == pytest.approx(0.80)
        assert result[1]["similarity_score"] == pytest.approx(0.60)

    @pytest.mark.asyncio
    async def test_fails_open_on_db_exception(
        self, retrieval_service, mock_db, tenant_id
    ):
        """Returns original chunks unchanged if feedback query raises."""
        chunks = [
            {
                "chunk_id": "c1",
                "document_id": "doc-1",
                "similarity_score": 0.75,
                "document_name": "doc1",
                "document_version": 1,
                "chunk_index": 0,
                "content": "content",
                "metadata": {},
            }
        ]

        mock_db.execute = AsyncMock(side_effect=RuntimeError("DB unavailable"))

        result = await retrieval_service._apply_feedback_weights(
            chunks=chunks, tenant_id=tenant_id
        )

        # Unchanged - fail open
        assert result[0]["similarity_score"] == pytest.approx(0.75)
