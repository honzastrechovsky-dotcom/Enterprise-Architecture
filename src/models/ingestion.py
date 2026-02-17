"""Ingestion job models for document processing pipeline.

IngestionJob tracks the complete lifecycle of document ingestion:
PENDING → PROCESSING → CHUNKING → EMBEDDING → INDEXING → COMPLETED/FAILED

DocumentChunk is already defined in document.py and will be reused.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class FileType(StrEnum):
    """Supported file types for ingestion."""

    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    HTML = "html"
    MARKDOWN = "md"
    TEXT = "txt"


class IngestionStatus(StrEnum):
    """Ingestion job status progression."""

    PENDING = "pending"
    PROCESSING = "processing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETED = "completed"
    FAILED = "failed"


class IngestionJob(Base):
    """Tracks document ingestion pipeline execution.

    Each upload creates an IngestionJob that progresses through stages:
    1. PENDING - Job created, waiting to start
    2. PROCESSING - Parsing file, extracting text
    3. CHUNKING - Splitting text into chunks
    4. EMBEDDING - Generating embeddings for chunks
    5. INDEXING - Storing chunks in vector database
    6. COMPLETED - Success
    7. FAILED - Error occurred (see error_message)
    """

    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # File information
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[FileType] = mapped_column(
        Enum(FileType, name="file_type"),
        nullable=False,
    )
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Job status
    status: Mapped[IngestionStatus] = mapped_column(
        Enum(IngestionStatus, name="ingestion_status"),
        nullable=False,
        default=IngestionStatus.PENDING,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Extracted metadata (title, author, date, page_count, etc.)
    metadata_extracted: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Document metadata extracted during parsing",
    )

    # Processing results
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of chunks created from this document",
    )

    # Timing information
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant")  # type: ignore[name-defined]

    __table_args__ = (
        Index("ix_ingestion_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_ingestion_jobs_tenant_created", "tenant_id", "created_at"),
        Index("ix_ingestion_jobs_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<IngestionJob id={self.id} filename={self.filename!r} status={self.status}>"
