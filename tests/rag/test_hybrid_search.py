"""Tests for hybrid_search module - semantic + lexical search with RRF fusion.

Tests cover:
- Semantic search scoring and ranking
- Lexical search scoring and ranking
- Reciprocal Rank Fusion (RRF) merging logic
- Empty results handling
- Tenant isolation
- Mock pgvector and text search calls
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.rag.hybrid_search import HybridSearchEngine, SearchResult


@pytest.fixture
def mock_db():
    """Mock async database session."""
    db = AsyncMock()
    return db


@pytest.fixture
def mock_llm():
    """Mock LLM client for embeddings."""
    llm = AsyncMock()
    llm.embed = AsyncMock(return_value=[[0.1] * 384])  # Mock 384-dim embedding
    return llm


@pytest.fixture
def settings():
    """Test settings."""
    return Settings(vector_top_k=10)


@pytest.fixture
def tenant_id():
    """Test tenant ID."""
    return uuid.uuid4()


@pytest.fixture
def engine(mock_db, settings, mock_llm):
    """HybridSearchEngine instance with mocks."""
    return HybridSearchEngine(
        db=mock_db,
        settings=settings,
        llm_client=mock_llm,
    )


class TestSemanticSearch:
    """Test semantic search scoring and ranking."""

    @pytest.mark.asyncio
    async def test_semantic_search_success(self, engine, tenant_id):
        """Test successful semantic search returns ranked results."""
        # Mock pgvector results
        mock_rows = [
            MagicMock(chunk_id=uuid.uuid4(), score=0.95),
            MagicMock(chunk_id=uuid.uuid4(), score=0.85),
            MagicMock(chunk_id=uuid.uuid4(), score=0.75),
        ]
        engine._db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=mock_rows)))

        results = await engine._semantic_search(
            query="test query",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert len(results) == 3
        assert results[0][1] == 0.95  # Highest score first
        assert results[1][1] == 0.85
        assert results[2][1] == 0.75

    @pytest.mark.asyncio
    async def test_semantic_search_embedding_failure(self, engine, tenant_id):
        """Test semantic search handles embedding failure gracefully."""
        engine._llm.embed = AsyncMock(side_effect=Exception("Embedding failed"))

        results = await engine._semantic_search(
            query="test query",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_semantic_search_db_failure(self, engine, tenant_id):
        """Test semantic search handles database failure gracefully."""
        engine._llm.embed = AsyncMock(return_value=[[0.1] * 384])
        engine._db.execute = AsyncMock(side_effect=Exception("DB error"))

        results = await engine._semantic_search(
            query="test query",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_semantic_search_empty_results(self, engine, tenant_id):
        """Test semantic search with no matching chunks."""
        engine._db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))

        results = await engine._semantic_search(
            query="test query",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_semantic_search_tenant_isolation(self, engine, tenant_id):
        """Test semantic search enforces tenant_id scoping."""
        engine._db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))

        await engine._semantic_search(
            query="test query",
            tenant_id=tenant_id,
            top_k=10,
        )

        # Verify tenant_id included in query
        call_args = engine._db.execute.call_args
        assert call_args[0][1]["tenant_id"] == tenant_id


class TestLexicalSearch:
    """Test lexical (full-text) search scoring and ranking."""

    @pytest.mark.asyncio
    async def test_lexical_search_success(self, engine, tenant_id):
        """Test successful lexical search returns ranked results."""
        mock_rows = [
            MagicMock(chunk_id=uuid.uuid4(), score=0.8),
            MagicMock(chunk_id=uuid.uuid4(), score=0.6),
            MagicMock(chunk_id=uuid.uuid4(), score=0.4),
        ]
        engine._db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=mock_rows)))

        results = await engine._lexical_search(
            query="test query",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert len(results) == 3
        assert results[0][1] == 0.8  # Highest score first
        assert results[1][1] == 0.6
        assert results[2][1] == 0.4

    @pytest.mark.asyncio
    async def test_lexical_search_db_failure(self, engine, tenant_id):
        """Test lexical search handles database failure gracefully."""
        engine._db.execute = AsyncMock(side_effect=Exception("DB error"))

        results = await engine._lexical_search(
            query="test query",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_lexical_search_empty_results(self, engine, tenant_id):
        """Test lexical search with no matching chunks."""
        engine._db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))

        results = await engine._lexical_search(
            query="test query",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert results == []


class TestReciprocalRankFusion:
    """Test RRF merging of semantic and lexical results."""

    @pytest.mark.asyncio
    async def test_rrf_fusion_combines_results(self, engine, tenant_id):
        """Test RRF correctly combines semantic and lexical results."""
        chunk_id_1 = uuid.uuid4()
        chunk_id_2 = uuid.uuid4()
        chunk_id_3 = uuid.uuid4()

        semantic_results = [
            (chunk_id_1, 0.95),  # Rank 0
            (chunk_id_2, 0.85),  # Rank 1
        ]

        lexical_results = [
            (chunk_id_2, 0.8),  # Rank 0
            (chunk_id_3, 0.6),  # Rank 1
        ]

        # Mock _fetch_chunks to return SearchResult objects in chunk_ids order
        search_results_by_id = {
            chunk_id_1: SearchResult(
                chunk_id=chunk_id_1,
                document_id=uuid.uuid4(),
                score=0.0,
                content="Content 1",
                chunk_index=0,
                metadata={},
                source="doc1.pdf",
                document_version="1.0",
            ),
            chunk_id_2: SearchResult(
                chunk_id=chunk_id_2,
                document_id=uuid.uuid4(),
                score=0.0,
                content="Content 2",
                chunk_index=0,
                metadata={},
                source="doc2.pdf",
                document_version="1.0",
            ),
            chunk_id_3: SearchResult(
                chunk_id=chunk_id_3,
                document_id=uuid.uuid4(),
                score=0.0,
                content="Content 3",
                chunk_index=0,
                metadata={},
                source="doc3.pdf",
                document_version="1.0",
            ),
        }

        async def mock_fetch_chunks(*, chunk_ids, scores, tenant_id):
            results = []
            for cid in chunk_ids:
                if cid in search_results_by_id:
                    r = search_results_by_id[cid]
                    r.score = scores[cid]
                    results.append(r)
            return results

        engine._fetch_chunks = mock_fetch_chunks

        fused = await engine._reciprocal_rank_fusion(
            semantic_results=semantic_results,
            lexical_results=lexical_results,
            tenant_id=tenant_id,
        )

        # chunk_id_2 appears in both (rank 1 semantic, rank 0 lexical) -> highest RRF score
        # RRF = 0.5/(60+1) + 0.5/(60+0) = 0.0082 + 0.0083 = 0.0165
        # chunk_id_1 only in semantic (rank 0) -> RRF = 0.5/(60+0) = 0.0083
        # chunk_id_3 only in lexical (rank 1) -> RRF = 0.5/(60+1) = 0.0082
        assert len(fused) == 3
        assert fused[0].chunk_id == chunk_id_2  # Highest RRF score

    @pytest.mark.asyncio
    async def test_rrf_fusion_semantic_only(self, engine, tenant_id):
        """Test RRF when only semantic results exist."""
        chunk_id_1 = uuid.uuid4()

        semantic_results = [(chunk_id_1, 0.95)]
        lexical_results = []

        mock_search_result = SearchResult(
            chunk_id=chunk_id_1,
            document_id=uuid.uuid4(),
            score=0.0,
            content="Content 1",
            chunk_index=0,
            metadata={},
            source="doc1.pdf",
            document_version="1.0",
        )

        engine._fetch_chunks = AsyncMock(return_value=[mock_search_result])

        fused = await engine._reciprocal_rank_fusion(
            semantic_results=semantic_results,
            lexical_results=lexical_results,
            tenant_id=tenant_id,
        )

        assert len(fused) == 1
        assert fused[0].chunk_id == chunk_id_1

    @pytest.mark.asyncio
    async def test_rrf_fusion_lexical_only(self, engine, tenant_id):
        """Test RRF when only lexical results exist."""
        chunk_id_1 = uuid.uuid4()

        semantic_results = []
        lexical_results = [(chunk_id_1, 0.8)]

        mock_search_result = SearchResult(
            chunk_id=chunk_id_1,
            document_id=uuid.uuid4(),
            score=0.0,
            content="Content 1",
            chunk_index=0,
            metadata={},
            source="doc1.pdf",
            document_version="1.0",
        )

        engine._fetch_chunks = AsyncMock(return_value=[mock_search_result])

        fused = await engine._reciprocal_rank_fusion(
            semantic_results=semantic_results,
            lexical_results=lexical_results,
            tenant_id=tenant_id,
        )

        assert len(fused) == 1
        assert fused[0].chunk_id == chunk_id_1

    @pytest.mark.asyncio
    async def test_rrf_fusion_empty_results(self, engine, tenant_id):
        """Test RRF with no results from either search."""
        fused = await engine._reciprocal_rank_fusion(
            semantic_results=[],
            lexical_results=[],
            tenant_id=tenant_id,
        )

        assert fused == []


class TestHybridSearch:
    """Test end-to-end hybrid search."""

    @pytest.mark.asyncio
    async def test_hybrid_search_success(self, engine, tenant_id):
        """Test successful hybrid search returns fused results."""
        chunk_id = uuid.uuid4()

        # Mock semantic search
        engine._semantic_search = AsyncMock(return_value=[(chunk_id, 0.9)])

        # Mock lexical search
        engine._lexical_search = AsyncMock(return_value=[(chunk_id, 0.8)])

        # Mock fusion
        mock_result = SearchResult(
            chunk_id=chunk_id,
            document_id=uuid.uuid4(),
            score=0.85,
            content="Test content",
            chunk_index=0,
            metadata={},
            source="test.pdf",
            document_version="1.0",
        )
        engine._reciprocal_rank_fusion = AsyncMock(return_value=[mock_result])

        results = await engine.search(
            query="test query",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert len(results) == 1
        assert results[0].chunk_id == chunk_id

    @pytest.mark.asyncio
    async def test_hybrid_search_empty_query(self, engine, tenant_id):
        """Test hybrid search with empty query returns nothing."""
        results = await engine.search(
            query="",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_hybrid_search_whitespace_query(self, engine, tenant_id):
        """Test hybrid search with whitespace-only query returns nothing."""
        results = await engine.search(
            query="   ",
            tenant_id=tenant_id,
            top_k=10,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_hybrid_search_default_top_k(self, engine, settings, tenant_id):
        """Test hybrid search uses settings.vector_top_k when top_k not specified."""
        engine._semantic_search = AsyncMock(return_value=[])
        engine._lexical_search = AsyncMock(return_value=[])
        engine._reciprocal_rank_fusion = AsyncMock(return_value=[])

        await engine.search(
            query="test query",
            tenant_id=tenant_id,
        )

        # Should fetch top_k * 2 for fusion
        engine._semantic_search.assert_called_once_with(
            query="test query",
            tenant_id=tenant_id,
            top_k=settings.vector_top_k * 2,
            document_ids=None,
        )
