"""Document ingestion pipeline.

Flow:
1. Receive uploaded file bytes + metadata
2. Extract text (PDF via pypdf, plain text as-is)
3. Chunk into overlapping token windows (512 tokens, 50 overlap)
4. Embed each chunk via LiteLLM embedding endpoint
5. Store DocumentChunk records with embeddings in pgvector
6. Update Document.status and Document.chunk_count

Design decisions:
- We process in batches of 32 chunks to avoid embedding API timeouts
- Chunking uses tiktoken for accurate token counting
- Overlap is needed for context continuity across chunk boundaries
- On failure, Document.status is set to FAILED with the error message
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import structlog
import tiktoken
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient
from src.config import Settings
from src.models.document import Document, DocumentChunk, DocumentStatus

log = structlog.get_logger(__name__)

_EMBED_BATCH_SIZE = 32
_TOKENIZER_NAME = "cl100k_base"  # Works for most OpenAI-compatible models


@dataclass
class ChunkResult:
    content: str
    chunk_index: int
    metadata: dict[str, Any]


def _extract_text_from_pdf(file_bytes: bytes) -> list[tuple[str, int]]:
    """Extract text from PDF, returning (text, page_number) tuples."""
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append((text, i))
    return pages


def _chunk_text(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    metadata: dict[str, Any] | None = None,
    chunk_index_offset: int = 0,
) -> list[ChunkResult]:
    """Chunk text into overlapping token windows.

    Uses tiktoken for accurate token counting. Returns chunks with
    their character offsets for precise citation location.
    """
    enc = tiktoken.get_encoding(_TOKENIZER_NAME)
    tokens = enc.encode(text)
    total_tokens = len(tokens)

    if total_tokens == 0:
        return []

    chunks: list[ChunkResult] = []
    start = 0
    chunk_idx = chunk_index_offset

    while start < total_tokens:
        end = min(start + chunk_size, total_tokens)
        chunk_tokens = tokens[start:end]
        chunk_text = enc.decode(chunk_tokens)

        # Calculate approximate character offsets
        prefix_tokens = tokens[:start]
        char_start = len(enc.decode(prefix_tokens))

        chunk_meta = dict(metadata or {})
        chunk_meta.update({
            "char_start": char_start,
            "char_end": char_start + len(chunk_text),
            "token_count": len(chunk_tokens),
        })

        chunks.append(ChunkResult(
            content=chunk_text,
            chunk_index=chunk_idx,
            metadata=chunk_meta,
        ))

        chunk_idx += 1
        if end >= total_tokens:
            break
        start = end - chunk_overlap  # Overlap with next chunk

    return chunks


class IngestionPipeline:
    """Orchestrates the document ingestion process."""

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        llm_client: LLMClient,
    ) -> None:
        self._db = db
        self._settings = settings
        self._llm = llm_client

    async def ingest_document(
        self,
        *,
        document: Document,
        file_bytes: bytes,
        content_type: str,
    ) -> int:
        """Ingest a document: extract, chunk, embed, store.

        Updates document.status during processing.
        Returns the number of chunks created.

        Raises any exception and sets document.status = FAILED.
        """
        log.info(
            "ingest.start",
            document_id=str(document.id),
            filename=document.filename,
            size_bytes=len(file_bytes),
        )

        document.status = DocumentStatus.PROCESSING
        await self._db.flush()

        try:
            chunks = await self._extract_and_chunk(
                file_bytes=file_bytes,
                content_type=content_type,
                document=document,
            )

            await self._embed_and_store(chunks=chunks, document=document)

            document.status = DocumentStatus.READY
            document.chunk_count = len(chunks)
            document.size_bytes = len(file_bytes)
            await self._db.flush()

            log.info(
                "ingest.complete",
                document_id=str(document.id),
                chunk_count=len(chunks),
            )
            return len(chunks)

        except Exception as exc:
            document.status = DocumentStatus.FAILED
            document.error_message = str(exc)[:1000]
            await self._db.flush()
            log.error(
                "ingest.failed",
                document_id=str(document.id),
                error=str(exc),
            )
            raise

    async def _extract_and_chunk(
        self,
        *,
        file_bytes: bytes,
        content_type: str,
        document: Document,
    ) -> list[ChunkResult]:
        """Extract text from file and split into chunks."""
        all_chunks: list[ChunkResult] = []

        if content_type == "application/pdf" or document.filename.lower().endswith(".pdf"):
            pages = _extract_text_from_pdf(file_bytes)
            for page_text, page_num in pages:
                page_chunks = _chunk_text(
                    page_text,
                    chunk_size=self._settings.chunk_size_tokens,
                    chunk_overlap=self._settings.chunk_overlap_tokens,
                    metadata={"page_number": page_num, "source": document.filename},
                    chunk_index_offset=len(all_chunks),
                )
                all_chunks.extend(page_chunks)
        else:
            # Plain text
            text = file_bytes.decode("utf-8", errors="replace")
            all_chunks = _chunk_text(
                text,
                chunk_size=self._settings.chunk_size_tokens,
                chunk_overlap=self._settings.chunk_overlap_tokens,
                metadata={"source": document.filename},
            )

        log.debug(
            "ingest.chunked",
            document_id=str(document.id),
            chunk_count=len(all_chunks),
        )
        return all_chunks

    async def _embed_and_store(
        self,
        *,
        chunks: list[ChunkResult],
        document: Document,
    ) -> None:
        """Embed chunks in batches and store in pgvector."""
        for batch_start in range(0, len(chunks), _EMBED_BATCH_SIZE):
            batch = chunks[batch_start : batch_start + _EMBED_BATCH_SIZE]
            texts = [c.content for c in batch]

            embeddings = await self._llm.embed(texts)

            for chunk_result, embedding in zip(batch, embeddings):
                db_chunk = DocumentChunk(
                    document_id=document.id,
                    tenant_id=document.tenant_id,
                    content=chunk_result.content,
                    chunk_index=chunk_result.chunk_index,
                    embedding=embedding,
                    chunk_metadata=chunk_result.metadata,
                )
                self._db.add(db_chunk)

            await self._db.flush()
            log.debug(
                "ingest.batch_embedded",
                document_id=str(document.id),
                batch_start=batch_start,
                batch_size=len(batch),
            )
