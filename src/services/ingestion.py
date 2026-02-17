"""Ingestion service - business logic for document ingestion pipeline.

This service orchestrates the complete document ingestion workflow:
1. Create ingestion job
2. Parse file (extract text + metadata)
3. Chunk text into overlapping windows
4. Generate embeddings for chunks
5. Store chunks in vector database
6. Update job status throughout
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient
from src.config import Settings
from src.core.policy import apply_tenant_filter
from src.ingestion.chunker import chunk_document
from src.ingestion.parsers import get_parser
from src.models.document import DocumentChunk
from src.models.ingestion import FileType, IngestionJob, IngestionStatus

log = structlog.get_logger(__name__)

_EMBED_BATCH_SIZE = 32


class IngestionService:
    """Service for document ingestion operations."""

    def __init__(self, db: AsyncSession, settings: Settings, llm_client: LLMClient) -> None:
        self._db = db
        self._settings = settings
        self._llm = llm_client

    async def create_job(
        self,
        *,
        tenant_id: uuid.UUID,
        filename: str,
        file_type: FileType,
        file_size: int,
    ) -> IngestionJob:
        """Create a new ingestion job.

        Args:
            tenant_id: Tenant ID for multi-tenancy scoping
            filename: Original filename
            file_type: Type of file (PDF, MARKDOWN, etc.)
            file_size: File size in bytes

        Returns:
            Created IngestionJob instance in PENDING status
        """
        job = IngestionJob(
            tenant_id=tenant_id,
            filename=filename,
            file_type=file_type,
            file_size_bytes=file_size,
            status=IngestionStatus.PENDING,
        )
        self._db.add(job)
        await self._db.flush()

        log.info(
            "ingestion.job.created",
            job_id=str(job.id),
            tenant_id=str(tenant_id),
            filename=filename,
            file_type=file_type.value,
        )

        return job

    async def process_job(
        self,
        *,
        job_id: uuid.UUID,
        file_bytes: bytes,
    ) -> IngestionJob:
        """Process an ingestion job through the complete pipeline.

        Flow:
        1. PROCESSING - Parse file, extract text and metadata
        2. CHUNKING - Split text into chunks
        3. EMBEDDING - Generate embeddings for chunks
        4. INDEXING - Store chunks in database
        5. COMPLETED - Mark job as complete

        Args:
            job_id: ID of the ingestion job to process
            file_bytes: Raw bytes of the uploaded file

        Returns:
            Updated IngestionJob with final status (COMPLETED or FAILED)

        Raises:
            ValueError: If job not found or already processed
        """
        # Load job
        result = await self._db.execute(
            select(IngestionJob).where(IngestionJob.id == job_id)
        )
        job = result.scalar_one_or_none()

        if job is None:
            raise ValueError(f"Ingestion job {job_id} not found")

        if job.status not in (IngestionStatus.PENDING, IngestionStatus.FAILED):
            raise ValueError(f"Job {job_id} is not in PENDING or FAILED status")

        # Update status to PROCESSING
        job.status = IngestionStatus.PROCESSING
        job.started_at = datetime.now(UTC)
        await self._db.flush()

        try:
            # Step 1: Parse file
            parsed = await self._parse_file(job, file_bytes)
            job.metadata_extracted = parsed["metadata"]

            # Step 2: Chunk text
            job.status = IngestionStatus.CHUNKING
            await self._db.flush()

            chunks = chunk_document(
                parsed["text"],
                chunk_size=self._settings.chunk_size_tokens,
                overlap=self._settings.chunk_overlap_tokens,
            )

            log.info(
                "ingestion.chunked",
                job_id=str(job.id),
                chunk_count=len(chunks),
            )

            # Step 3: Generate embeddings
            job.status = IngestionStatus.EMBEDDING
            await self._db.flush()

            # Step 4: Store chunks with embeddings
            job.status = IngestionStatus.INDEXING
            await self._db.flush()

            await self._embed_and_store_chunks(job, chunks)

            # Step 5: Mark complete
            job.status = IngestionStatus.COMPLETED
            job.chunk_count = len(chunks)
            job.completed_at = datetime.now(UTC)
            await self._db.flush()

            log.info(
                "ingestion.completed",
                job_id=str(job.id),
                chunk_count=len(chunks),
            )

            return job

        except Exception as exc:
            # Mark job as failed
            job.status = IngestionStatus.FAILED
            job.error_message = str(exc)[:1000]  # Truncate error messages
            job.completed_at = datetime.now(UTC)
            await self._db.flush()

            log.error(
                "ingestion.failed",
                job_id=str(job.id),
                error=str(exc),
                exc_info=True,
            )

            raise

    async def get_job(self, job_id: uuid.UUID, tenant_id: uuid.UUID) -> IngestionJob | None:
        """Get an ingestion job by ID (tenant-scoped).

        Args:
            job_id: Job ID to retrieve
            tenant_id: Tenant ID for authorization

        Returns:
            IngestionJob if found, None otherwise
        """
        stmt = apply_tenant_filter(
            select(IngestionJob).where(IngestionJob.id == job_id),
            IngestionJob,
            tenant_id,
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_jobs(
        self,
        tenant_id: uuid.UUID,
        status: IngestionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IngestionJob]:
        """List ingestion jobs for a tenant.

        Args:
            tenant_id: Tenant ID to filter by
            status: Optional status filter
            limit: Maximum number of jobs to return
            offset: Offset for pagination

        Returns:
            List of IngestionJob instances
        """
        stmt = apply_tenant_filter(
            select(IngestionJob).order_by(IngestionJob.created_at.desc()),
            IngestionJob,
            tenant_id,
        )

        if status:
            stmt = stmt.where(IngestionJob.status == status)

        stmt = stmt.limit(limit).offset(offset)

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def cancel_job(self, job_id: uuid.UUID, tenant_id: uuid.UUID) -> IngestionJob:
        """Cancel a pending or processing ingestion job.

        Args:
            job_id: Job ID to cancel
            tenant_id: Tenant ID for authorization

        Returns:
            Updated IngestionJob with FAILED status

        Raises:
            ValueError: If job not found or cannot be cancelled
        """
        job = await self.get_job(job_id, tenant_id)

        if job is None:
            raise ValueError(f"Job {job_id} not found")

        if job.status in (IngestionStatus.COMPLETED, IngestionStatus.FAILED):
            raise ValueError(f"Cannot cancel job in {job.status} status")

        job.status = IngestionStatus.FAILED
        job.error_message = "Cancelled by user"
        job.completed_at = datetime.now(UTC)
        await self._db.flush()

        log.info("ingestion.cancelled", job_id=str(job.id))

        return job

    async def get_job_chunks(
        self,
        job_id: uuid.UUID,
        tenant_id: uuid.UUID,
        limit: int = 100,
    ) -> list[DocumentChunk]:
        """Get chunks created by an ingestion job.

        Note: This requires linking IngestionJob to Document,
        or storing job_id in DocumentChunk metadata.

        For now, we'll return chunks based on metadata matching.

        Args:
            job_id: Job ID to get chunks for
            tenant_id: Tenant ID for authorization
            limit: Maximum chunks to return

        Returns:
            List of DocumentChunk instances
        """
        # Get the job
        job = await self.get_job(job_id, tenant_id)
        if job is None:
            return []

        # For now, we'll match based on tenant and creation time
        # In a real implementation, we'd link job_id to document_id
        # or store job_id in chunk metadata

        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.tenant_id == tenant_id)
            .order_by(DocumentChunk.created_at.desc())
            .limit(limit)
        )

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    # Private helper methods

    async def _parse_file(self, job: IngestionJob, file_bytes: bytes) -> dict[str, Any]:
        """Parse file and extract text and metadata."""
        # Write file to temporary location for parsing
        with tempfile.NamedTemporaryFile(
            suffix=f".{job.file_type.value}",
            delete=False,
        ) as tmp_file:
            tmp_file.write(file_bytes)
            tmp_path = tmp_file.name

        try:
            parser = get_parser(job.file_type)
            result = await parser.parse(tmp_path)

            return {
                "text": result.text,
                "metadata": result.metadata,
                "sections": result.sections,
            }
        finally:
            # Clean up temp file
            Path(tmp_path).unlink(missing_ok=True)

    async def _embed_and_store_chunks(
        self,
        job: IngestionJob,
        chunks: list,  # List of Chunk objects from chunker
    ) -> None:
        """Generate embeddings and store chunks in database."""
        # Process chunks in batches
        for batch_start in range(0, len(chunks), _EMBED_BATCH_SIZE):
            batch = chunks[batch_start : batch_start + _EMBED_BATCH_SIZE]
            texts = [c.content for c in batch]

            # Generate embeddings
            embeddings = await self._llm.embed(texts)

            # Store chunks
            for chunk, embedding in zip(batch, embeddings):
                db_chunk = DocumentChunk(
                    document_id=uuid.uuid4(),  # Chunk-level ID; IngestionJob has no document FK yet
                    tenant_id=job.tenant_id,
                    content=chunk.content,
                    chunk_index=chunk.index,
                    embedding=embedding,
                    chunk_metadata={
                        "ingestion_job_id": str(job.id),
                        "token_count": chunk.token_count,
                        **chunk.metadata,
                        **job.metadata_extracted,
                    },
                )
                self._db.add(db_chunk)

            await self._db.flush()

            log.debug(
                "ingestion.batch_stored",
                job_id=str(job.id),
                batch_start=batch_start,
                batch_size=len(batch),
            )
