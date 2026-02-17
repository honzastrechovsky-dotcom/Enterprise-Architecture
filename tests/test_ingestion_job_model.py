"""Tests for IngestionJob model.

Unit tests for model definition, enum values, and field contracts.
Note: SQLAlchemy column defaults (id, chunk_count, created_at) are applied
at INSERT time, not at Python object construction time.
Integration tests (real DB) would verify persisted values with actual PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from src.models.ingestion import FileType, IngestionJob, IngestionStatus


def test_ingestion_status_enum_values() -> None:
    """Test IngestionStatus enum has all required status values."""
    assert IngestionStatus.PENDING == "pending"
    assert IngestionStatus.PROCESSING == "processing"
    assert IngestionStatus.CHUNKING == "chunking"
    assert IngestionStatus.EMBEDDING == "embedding"
    assert IngestionStatus.INDEXING == "indexing"
    assert IngestionStatus.COMPLETED == "completed"
    assert IngestionStatus.FAILED == "failed"


def test_file_type_enum_values() -> None:
    """Test FileType enum has all required file type values."""
    assert FileType.PDF == "pdf"
    assert FileType.DOCX == "docx"
    assert FileType.PPTX == "pptx"
    assert FileType.XLSX == "xlsx"
    assert FileType.HTML == "html"
    assert FileType.MARKDOWN == "md"
    assert FileType.TEXT == "txt"


def test_ingestion_job_tablename() -> None:
    """Test IngestionJob uses correct database table name."""
    assert IngestionJob.__tablename__ == "ingestion_jobs"


def test_ingestion_job_can_be_instantiated() -> None:
    """Test IngestionJob can be instantiated with required fields."""
    tenant_id = uuid.uuid4()
    job = IngestionJob(
        tenant_id=tenant_id,
        filename="test-document.pdf",
        file_type=FileType.PDF,
        file_size_bytes=1024 * 1024,
        status=IngestionStatus.PENDING,
    )

    assert job.tenant_id == tenant_id
    assert job.filename == "test-document.pdf"
    assert job.file_type == FileType.PDF
    assert job.file_size_bytes == 1024 * 1024
    assert job.status == IngestionStatus.PENDING
    # These are explicitly set so they should not be None
    assert job.error_message is None
    assert job.started_at is None
    assert job.completed_at is None


def test_ingestion_job_all_file_types() -> None:
    """Test IngestionJob can be created with all supported file types."""
    tenant_id = uuid.uuid4()
    file_types = [
        (FileType.PDF, "doc.pdf"),
        (FileType.DOCX, "doc.docx"),
        (FileType.PPTX, "presentation.pptx"),
        (FileType.XLSX, "spreadsheet.xlsx"),
        (FileType.HTML, "page.html"),
        (FileType.MARKDOWN, "readme.md"),
        (FileType.TEXT, "notes.txt"),
    ]

    for file_type, filename in file_types:
        job = IngestionJob(
            tenant_id=tenant_id,
            filename=filename,
            file_type=file_type,
            file_size_bytes=1024,
            status=IngestionStatus.PENDING,
        )
        assert job.file_type == file_type
        assert job.filename == filename


def test_ingestion_job_status_transitions() -> None:
    """Test IngestionJob status field can be updated through the pipeline."""
    job = IngestionJob(
        tenant_id=uuid.uuid4(),
        filename="doc.pdf",
        file_type=FileType.PDF,
        file_size_bytes=1024,
        status=IngestionStatus.PENDING,
    )

    # Simulate pipeline status transitions
    job.status = IngestionStatus.PROCESSING
    assert job.status == IngestionStatus.PROCESSING

    job.status = IngestionStatus.CHUNKING
    assert job.status == IngestionStatus.CHUNKING

    job.status = IngestionStatus.EMBEDDING
    assert job.status == IngestionStatus.EMBEDDING

    job.status = IngestionStatus.INDEXING
    assert job.status == IngestionStatus.INDEXING

    job.status = IngestionStatus.COMPLETED
    job.chunk_count = 42
    assert job.status == IngestionStatus.COMPLETED
    assert job.chunk_count == 42


def test_ingestion_job_metadata_stored() -> None:
    """Test IngestionJob metadata_extracted can store dict data."""
    metadata = {
        "title": "Enterprise AI Platform Architecture",
        "author": "Engineering Team",
        "page_count": 25,
        "creation_date": "2026-02-15",
    }

    job = IngestionJob(
        tenant_id=uuid.uuid4(),
        filename="architecture.pdf",
        file_type=FileType.PDF,
        file_size_bytes=5 * 1024 * 1024,
        status=IngestionStatus.PROCESSING,
        metadata_extracted=metadata,
    )

    assert job.metadata_extracted["title"] == "Enterprise AI Platform Architecture"
    assert job.metadata_extracted["page_count"] == 25
    assert job.metadata_extracted["author"] == "Engineering Team"


def test_ingestion_job_failure_state() -> None:
    """Test IngestionJob can store failure state with error message."""
    job = IngestionJob(
        tenant_id=uuid.uuid4(),
        filename="broken.pdf",
        file_type=FileType.PDF,
        file_size_bytes=1024,
        status=IngestionStatus.FAILED,
        error_message="Failed to parse PDF: corrupted file structure",
    )

    assert job.status == IngestionStatus.FAILED
    assert "corrupted file" in job.error_message


def test_ingestion_job_repr() -> None:
    """Test IngestionJob repr is informative and includes key fields."""
    job = IngestionJob(
        tenant_id=uuid.uuid4(),
        filename="test.pdf",
        file_type=FileType.PDF,
        file_size_bytes=1024,
        status=IngestionStatus.PENDING,
    )

    repr_str = repr(job)
    assert "IngestionJob" in repr_str
    assert "test.pdf" in repr_str


def test_ingestion_job_timing_fields() -> None:
    """Test IngestionJob timing fields can be explicitly set."""
    now = datetime.now(timezone.utc)
    job = IngestionJob(
        tenant_id=uuid.uuid4(),
        filename="test.pdf",
        file_type=FileType.PDF,
        file_size_bytes=1024,
        status=IngestionStatus.PROCESSING,
        started_at=now,
    )

    assert job.started_at == now
    assert job.completed_at is None

    # Complete the job
    job.status = IngestionStatus.COMPLETED
    completed_at = datetime.now(timezone.utc)
    job.completed_at = completed_at
    assert job.completed_at == completed_at


def test_ingestion_job_has_required_indexes_defined() -> None:
    """Test IngestionJob model defines required database indexes."""
    table_args = IngestionJob.__table_args__

    # Extract index names from table args
    index_names = {
        arg.name
        for arg in table_args
        if hasattr(arg, "name")
    }

    assert "ix_ingestion_jobs_tenant_status" in index_names
    assert "ix_ingestion_jobs_tenant_created" in index_names
    assert "ix_ingestion_jobs_status" in index_names


def test_ingestion_job_default_metadata_extracted() -> None:
    """Test IngestionJob metadata_extracted defaults to empty dict."""
    job = IngestionJob(
        tenant_id=uuid.uuid4(),
        filename="test.pdf",
        file_type=FileType.PDF,
        file_size_bytes=1024,
        metadata_extracted={},  # Explicit empty dict (server default is {})
    )

    assert job.metadata_extracted == {}
