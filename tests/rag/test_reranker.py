"""Tests for reranker module - LLM-based relevance scoring.

Tests cover:
- Reranker scores and reorders results
- Empty input handling
- Top-k limiting
- LLM failure handling
- Score normalization
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.rag.reranker import CrossEncoderReranker, RankedResult


@dataclass
class MockSearchResult:
    """Mock SearchResult for testing."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    score: float
    content: str
    chunk_index: int
    metadata: dict
    source: str
    document_version: str


@pytest.fixture
def mock_llm():
    """Mock LLM client."""
    llm = AsyncMock()
    llm.complete = AsyncMock()
    llm.extract_text = MagicMock()
    return llm


@pytest.fixture
def reranker(mock_llm):
    """CrossEncoderReranker instance with mock LLM."""
    return CrossEncoderReranker(llm_client=mock_llm)


class TestReranker:
    """Test reranker scoring and reordering."""

    @pytest.mark.asyncio
    async def test_rerank_success(self, reranker):
        """Test reranker successfully scores and reorders results."""
        results = [
            MockSearchResult(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                score=0.5,
                content="Less relevant content",
                chunk_index=0,
                metadata={},
                source="doc1.pdf",
                document_version="1.0",
            ),
            MockSearchResult(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                score=0.9,
                content="Highly relevant content",
                chunk_index=0,
                metadata={},
                source="doc2.pdf",
                document_version="1.0",
            ),
        ]

        # Mock LLM to return scores: 3/10 for first, 9/10 for second
        reranker._llm.complete = AsyncMock(return_value=MagicMock())
        reranker._llm.extract_text = MagicMock(side_effect=["3", "9"])

        ranked = await reranker.rerank(
            query="test query",
            results=results,
        )

        assert len(ranked) == 2
        # Should be reordered by relevance score (9/10 first)
        assert ranked[0].relevance_score == 0.9  # 9/10 normalized
        assert ranked[1].relevance_score == 0.3  # 3/10 normalized
        assert ranked[0].content == "Highly relevant content"

    @pytest.mark.asyncio
    async def test_rerank_empty_results(self, reranker):
        """Test reranker handles empty input."""
        ranked = await reranker.rerank(
            query="test query",
            results=[],
        )

        assert ranked == []

    @pytest.mark.asyncio
    async def test_rerank_top_k_limiting(self, reranker):
        """Test reranker respects top_k limit."""
        results = [
            MockSearchResult(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                score=0.5,
                content=f"Content {i}",
                chunk_index=i,
                metadata={},
                source=f"doc{i}.pdf",
                document_version="1.0",
            )
            for i in range(10)
        ]

        # Mock LLM to return descending scores
        reranker._llm.complete = AsyncMock(return_value=MagicMock())
        reranker._llm.extract_text = MagicMock(
            side_effect=[str(10 - i) for i in range(10)]
        )

        ranked = await reranker.rerank(
            query="test query",
            results=results,
            top_k=3,
        )

        assert len(ranked) == 3
        assert ranked[0].relevance_score == 1.0  # 10/10
        assert ranked[1].relevance_score == 0.9  # 9/10
        assert ranked[2].relevance_score == 0.8  # 8/10

    @pytest.mark.asyncio
    async def test_rerank_llm_failure(self, reranker):
        """Test reranker handles LLM failures gracefully."""
        results = [
            MockSearchResult(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                score=0.5,
                content="Content 1",
                chunk_index=0,
                metadata={},
                source="doc1.pdf",
                document_version="1.0",
            ),
        ]

        # Mock LLM to fail
        reranker._llm.complete = AsyncMock(side_effect=Exception("LLM error"))

        ranked = await reranker.rerank(
            query="test query",
            results=results,
        )

        # Should return results with default neutral score (0.5)
        assert len(ranked) == 1
        assert ranked[0].relevance_score == 0.5

    @pytest.mark.asyncio
    async def test_rerank_invalid_score_format(self, reranker):
        """Test reranker handles invalid LLM score responses."""
        results = [
            MockSearchResult(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                score=0.5,
                content="Content 1",
                chunk_index=0,
                metadata={},
                source="doc1.pdf",
                document_version="1.0",
            ),
        ]

        # Mock LLM to return non-numeric response
        reranker._llm.complete = AsyncMock(return_value=MagicMock())
        reranker._llm.extract_text = MagicMock(return_value="not a number")

        ranked = await reranker.rerank(
            query="test query",
            results=results,
        )

        # Should default to 0.5 on parse failure
        assert len(ranked) == 1
        assert ranked[0].relevance_score == 0.5

    @pytest.mark.asyncio
    async def test_rerank_score_normalization(self, reranker):
        """Test reranker correctly normalizes scores to 0-1 range."""
        results = [
            MockSearchResult(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                score=0.5,
                content="Content 1",
                chunk_index=0,
                metadata={},
                source="doc1.pdf",
                document_version="1.0",
            ),
        ]

        # Test boundary cases
        test_cases = [
            ("0", 0.0),  # Min score
            ("10", 1.0),  # Max score
            ("5", 0.5),  # Mid score
            ("-1", 0.0),  # Below min (clamped)
            ("15", 1.0),  # Above max (clamped)
        ]

        for llm_score, expected_normalized in test_cases:
            reranker._llm.complete = AsyncMock(return_value=MagicMock())
            reranker._llm.extract_text = MagicMock(return_value=llm_score)

            ranked = await reranker.rerank(
                query="test query",
                results=results,
            )

            assert ranked[0].relevance_score == expected_normalized

    @pytest.mark.asyncio
    async def test_rerank_preserves_metadata(self, reranker):
        """Test reranker preserves all result metadata."""
        chunk_id = uuid.uuid4()
        doc_id = uuid.uuid4()
        metadata = {"key": "value"}

        results = [
            MockSearchResult(
                chunk_id=chunk_id,
                document_id=doc_id,
                score=0.5,
                content="Test content",
                chunk_index=3,
                metadata=metadata,
                source="test.pdf",
                document_version="2.0",
            ),
        ]

        reranker._llm.complete = AsyncMock(return_value=MagicMock())
        reranker._llm.extract_text = MagicMock(return_value="8")

        ranked = await reranker.rerank(
            query="test query",
            results=results,
        )

        assert ranked[0].chunk_id == chunk_id
        assert ranked[0].document_id == doc_id
        assert ranked[0].original_score == 0.5
        assert ranked[0].content == "Test content"
        assert ranked[0].chunk_index == 3
        assert ranked[0].metadata == metadata
        assert ranked[0].source == "test.pdf"
        assert ranked[0].document_version == "2.0"

    @pytest.mark.asyncio
    async def test_rerank_content_truncation(self, reranker):
        """Test reranker truncates long content to avoid token limits."""
        long_content = "x" * 5000  # Content longer than 2000 chars

        results = [
            MockSearchResult(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                score=0.5,
                content=long_content,
                chunk_index=0,
                metadata={},
                source="doc1.pdf",
                document_version="1.0",
            ),
        ]

        reranker._llm.complete = AsyncMock(return_value=MagicMock())
        reranker._llm.extract_text = MagicMock(return_value="7")

        await reranker.rerank(
            query="test query",
            results=results,
        )

        # Verify LLM was called with truncated content (max 2000 chars)
        call_args = reranker._llm.complete.call_args
        prompt = call_args[1]["messages"][0]["content"]
        # Content should be truncated in the prompt
        assert len(prompt) < len(long_content) + 1000  # Some overhead for prompt template
