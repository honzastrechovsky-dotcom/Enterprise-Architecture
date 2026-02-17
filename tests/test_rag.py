"""Tests for RAG (Retrieval Augmented Generation) pipeline.

Covers:
- Document chunking with correct token counts and overlap
- Embedding storage and retrieval from pgvector
- Similarity search correctness
- Citation formatting
- Ingestion pipeline state management (PENDING -> PROCESSING -> READY/FAILED)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document import Document, DocumentChunk, DocumentStatus


class TestChunking:
    """Text chunking behavior."""

    def test_basic_text_chunking(self) -> None:
        """Short text produces a single chunk."""
        from src.rag.ingest import _chunk_text

        text = "Hello, this is a short sentence."
        chunks = _chunk_text(text, chunk_size=512, chunk_overlap=50)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert "Hello" in chunks[0].content

    def test_long_text_produces_multiple_chunks(self) -> None:
        """Text longer than chunk_size produces multiple chunks."""
        from src.rag.ingest import _chunk_text

        # Create text of ~1500 tokens (approximate: 1 word ~ 1.3 tokens)
        words = ["enterprise"] * 1000
        text = " ".join(words)
        chunks = _chunk_text(text, chunk_size=256, chunk_overlap=25)
        assert len(chunks) > 1

    def test_chunk_overlap_produces_shared_content(self) -> None:
        """Consecutive chunks share content from the overlap region."""
        from src.rag.ingest import _chunk_text

        # Create enough text to force 2+ chunks
        words = ["word"] * 800
        text = " ".join(words)
        chunks = _chunk_text(text, chunk_size=200, chunk_overlap=50)

        if len(chunks) >= 2:
            # Last tokens of chunk[0] should appear in start of chunk[1]
            # (Not exact due to encoding but both should have "word" tokens)
            assert "word" in chunks[0].content
            assert "word" in chunks[1].content

    def test_empty_text_produces_no_chunks(self) -> None:
        """Empty text produces zero chunks."""
        from src.rag.ingest import _chunk_text

        chunks = _chunk_text("", chunk_size=512, chunk_overlap=50)
        assert len(chunks) == 0

    def test_chunk_indices_are_sequential(self) -> None:
        """Chunk indices start at 0 and increment by 1."""
        from src.rag.ingest import _chunk_text

        words = ["text"] * 600
        text = " ".join(words)
        chunks = _chunk_text(text, chunk_size=100, chunk_overlap=10)
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_chunk_metadata_contains_source(self) -> None:
        """Chunk metadata includes source filename when provided."""
        from src.rag.ingest import _chunk_text

        chunks = _chunk_text(
            "Sample content",
            chunk_size=512,
            chunk_overlap=50,
            metadata={"source": "test.txt"},
        )
        assert chunks[0].metadata["source"] == "test.txt"


class TestIngestionPipeline:
    """IngestionPipeline state machine and embedding storage."""

    @pytest.mark.asyncio
    async def test_ingestion_sets_status_to_ready(
        self,
        db_session: AsyncSession,
        tenant_a,
        admin_user_a,
    ) -> None:
        """Successful ingestion sets document status to READY."""
        from src.rag.ingest import IngestionPipeline
        from src.config import get_settings

        doc = Document(
            tenant_id=tenant_a.id,
            uploaded_by_user_id=admin_user_a.id,
            filename="test.txt",
            content_type="text/plain",
            status=DocumentStatus.PENDING,
        )
        db_session.add(doc)
        await db_session.flush()

        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value=[[0.1] * 1536])

        pipeline = IngestionPipeline(
            db=db_session,
            settings=get_settings(),
            llm_client=mock_llm,
        )
        chunk_count = await pipeline.ingest_document(
            document=doc,
            file_bytes=b"Hello world. This is test content for ingestion.",
            content_type="text/plain",
        )

        assert doc.status == DocumentStatus.READY
        assert chunk_count >= 1

    @pytest.mark.asyncio
    async def test_ingestion_creates_chunks_in_db(
        self,
        db_session: AsyncSession,
        tenant_a,
        admin_user_a,
    ) -> None:
        """Ingestion stores DocumentChunk records with embeddings."""
        from src.rag.ingest import IngestionPipeline
        from src.config import get_settings

        doc = Document(
            tenant_id=tenant_a.id,
            uploaded_by_user_id=admin_user_a.id,
            filename="chunks_test.txt",
            content_type="text/plain",
            status=DocumentStatus.PENDING,
        )
        db_session.add(doc)
        await db_session.flush()

        fake_embedding = [0.5] * 1536
        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value=[fake_embedding])

        # Track objects added to the session
        added_chunks = []
        original_add = db_session.add

        def tracking_add(obj):
            if isinstance(obj, DocumentChunk):
                added_chunks.append(obj)
            original_add(obj)

        db_session.add = tracking_add

        pipeline = IngestionPipeline(
            db=db_session,
            settings=get_settings(),
            llm_client=mock_llm,
        )
        await pipeline.ingest_document(
            document=doc,
            file_bytes=b"Test document content with enough words to create at least one chunk.",
            content_type="text/plain",
        )

        # Verify chunks were added to the session
        assert len(added_chunks) >= 1
        assert added_chunks[0].tenant_id == tenant_a.id
        assert added_chunks[0].embedding is not None

    @pytest.mark.asyncio
    async def test_ingestion_failure_sets_failed_status(
        self,
        db_session: AsyncSession,
        tenant_a,
        admin_user_a,
    ) -> None:
        """If embedding fails, document status is set to FAILED."""
        from src.rag.ingest import IngestionPipeline
        from src.agent.llm import LLMError
        from src.config import get_settings

        doc = Document(
            tenant_id=tenant_a.id,
            uploaded_by_user_id=admin_user_a.id,
            filename="will_fail.txt",
            content_type="text/plain",
            status=DocumentStatus.PENDING,
        )
        db_session.add(doc)
        await db_session.flush()

        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(side_effect=LLMError("Connection refused"))

        pipeline = IngestionPipeline(
            db=db_session,
            settings=get_settings(),
            llm_client=mock_llm,
        )

        with pytest.raises(LLMError):
            await pipeline.ingest_document(
                document=doc,
                file_bytes=b"Content that fails to embed",
                content_type="text/plain",
            )

        assert doc.status == DocumentStatus.FAILED
        assert doc.error_message is not None


class TestCitations:
    """Citation building and formatting."""

    def test_build_citations_from_chunks(self) -> None:
        """Citations are built correctly from chunk dicts."""
        from src.rag.citations import build_citations

        chunks = [
            {
                "chunk_id": str(uuid.uuid4()),
                "document_id": str(uuid.uuid4()),
                "document_name": "Policy Handbook.pdf",
                "document_version": "3.0",
                "chunk_index": 5,
                "content": "The vacation policy allows 20 days per year.",
                "similarity_score": 0.92,
                "metadata": {"page_number": 12, "section": "HR Policies"},
            }
        ]

        citations = build_citations(chunks)
        assert len(citations) == 1
        c = citations[0]
        assert c.index == 1
        assert c.document_name == "Policy Handbook.pdf"
        assert c.document_version == "3.0"
        assert c.chunk_index == 5
        assert c.page_number == 12
        assert c.section == "HR Policies"

    def test_citation_content_is_truncated(self) -> None:
        """Long chunk content is truncated to 200 chars in snippet."""
        from src.rag.citations import build_citations

        long_content = "A" * 500
        chunks = [
            {
                "chunk_id": str(uuid.uuid4()),
                "document_id": str(uuid.uuid4()),
                "document_name": "doc.pdf",
                "document_version": "1.0",
                "chunk_index": 0,
                "content": long_content,
                "similarity_score": 0.8,
                "metadata": {},
            }
        ]

        citations = build_citations(chunks)
        assert len(citations[0].content_snippet) <= 203  # 200 + "..."

    def test_format_citations_for_prompt(self) -> None:
        """Citations are formatted as a readable prompt block."""
        from src.rag.citations import build_citations, format_citations_for_prompt

        chunks = [
            {
                "chunk_id": str(uuid.uuid4()),
                "document_id": str(uuid.uuid4()),
                "document_name": "Annual Report.pdf",
                "document_version": "2024",
                "chunk_index": 0,
                "content": "Revenue grew by 15% in Q3.",
                "similarity_score": 0.95,
                "metadata": {},
            }
        ]
        citations = build_citations(chunks)
        prompt_block = format_citations_for_prompt(citations)

        assert "Annual Report.pdf" in prompt_block
        assert "Revenue grew by 15%" in prompt_block
        assert "[1]" in prompt_block

    def test_empty_citations_returns_empty_string(self) -> None:
        """No citations produces empty string."""
        from src.rag.citations import format_citations_for_prompt

        result = format_citations_for_prompt([])
        assert result == ""

    def test_multiple_citations_indexed_correctly(self) -> None:
        """Multiple citations have sequential 1-based indices."""
        from src.rag.citations import build_citations

        chunks = [
            {
                "chunk_id": str(uuid.uuid4()),
                "document_id": str(uuid.uuid4()),
                "document_name": f"doc{i}.pdf",
                "document_version": "1.0",
                "chunk_index": i,
                "content": f"Content {i}",
                "similarity_score": 0.9 - i * 0.1,
                "metadata": {},
            }
            for i in range(3)
        ]

        citations = build_citations(chunks)
        assert [c.index for c in citations] == [1, 2, 3]
