"""Document ingestion API endpoints.

Provides full lifecycle management for document ingestion jobs.

POST /api/v1/documents/upload         - Upload file and create ingestion job
GET  /api/v1/documents/jobs           - List ingestion jobs
GET  /api/v1/documents/jobs/{id}      - Get job status
POST /api/v1/documents/jobs/{id}/cancel - Cancel a job
GET  /api/v1/documents/jobs/{id}/chunks - Get chunks from completed job

All endpoints are scoped to the authenticated user's tenant.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient
from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.config import Settings, get_settings
from src.core.audit import AuditService
from src.core.policy import Permission, check_permission
from src.database import get_db_session
from src.models.audit import AuditStatus
from src.models.ingestion import FileType, IngestionJob, IngestionStatus
from src.services.ingestion import IngestionService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["ingestion"])

# File validation constants
_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

# Mapping of MIME types to FileType enum
_MIME_TYPE_MAP: dict[str, FileType] = {
    "application/pdf": FileType.PDF,
    "text/plain": FileType.TEXT,
    "text/markdown": FileType.MARKDOWN,
    "text/x-markdown": FileType.MARKDOWN,
    "text/html": FileType.HTML,
    "text/htm": FileType.HTML,
    # DOCX/PPTX/XLSX have limited parsing support (added for type detection)
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCX,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": FileType.PPTX,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": FileType.XLSX,
    "application/msword": FileType.DOCX,
}

# Extension to FileType mapping (fallback when MIME type is missing)
_EXTENSION_MAP: dict[str, FileType] = {
    ".pdf": FileType.PDF,
    ".txt": FileType.TEXT,
    ".md": FileType.MARKDOWN,
    ".markdown": FileType.MARKDOWN,
    ".html": FileType.HTML,
    ".htm": FileType.HTML,
    ".docx": FileType.DOCX,
    ".pptx": FileType.PPTX,
    ".xlsx": FileType.XLSX,
}

# Types that have parsing support (others create job but fail at parse)
_PARSEABLE_TYPES = {FileType.PDF, FileType.TEXT, FileType.MARKDOWN, FileType.HTML}


# ------------------------------------------------------------------ #
# Response models
# ------------------------------------------------------------------ #


class IngestionJobResponse(BaseModel):
    """Response model for ingestion job."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    filename: str
    file_type: str
    file_size_bytes: int
    status: str
    error_message: str | None
    metadata_extracted: dict[str, Any]
    chunk_count: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class IngestionJobListResponse(BaseModel):
    """Response model for list of ingestion jobs."""

    jobs: list[IngestionJobResponse]
    total: int


class ChunkResponse(BaseModel):
    """Response model for a document chunk."""

    id: uuid.UUID
    chunk_index: int
    content: str
    token_count: int | None
    metadata: dict[str, Any]

    model_config = {"from_attributes": True}


# ------------------------------------------------------------------ #
# Helper functions
# ------------------------------------------------------------------ #


def _resolve_file_type(filename: str, content_type: str | None) -> FileType:
    """Determine file type from MIME type or extension."""
    # Try MIME type first
    if content_type and content_type in _MIME_TYPE_MAP:
        return _MIME_TYPE_MAP[content_type]

    # Fall back to extension
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix in _EXTENSION_MAP:
        return _EXTENSION_MAP[suffix]

    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail=(
            f"Unsupported file type. Supported: "
            f"{', '.join(sorted(ext for ext in _EXTENSION_MAP))}"
        ),
    )


def _job_to_response(job: IngestionJob) -> IngestionJobResponse:
    """Convert IngestionJob model to response."""
    return IngestionJobResponse(
        id=job.id,
        tenant_id=job.tenant_id,
        filename=job.filename,
        file_type=job.file_type.value,
        file_size_bytes=job.file_size_bytes,
        status=job.status.value,
        error_message=job.error_message,
        metadata_extracted=job.metadata_extracted,
        chunk_count=job.chunk_count,
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
    )


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #


@router.post(
    "/upload",
    response_model=IngestionJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document and start ingestion job",
)
async def upload_document(
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestionJobResponse:
    """Upload a document for ingestion.

    Creates an ingestion job and starts processing asynchronously.
    Returns the job with PROCESSING or COMPLETED status depending on
    how quickly it completes.

    Supported file types:
    - PDF (.pdf)
    - Markdown (.md)
    - Plain text (.txt)
    - HTML (.html, .htm)
    - DOCX (.docx) - creates job, basic support
    - PPTX (.pptx) - creates job, basic support
    - XLSX (.xlsx) - creates job, basic support

    Max file size: 50MB
    """
    check_permission(current_user.role, Permission.DOCUMENT_UPLOAD)

    filename = file.filename or "unknown"

    # Validate and detect file type
    file_type = _resolve_file_type(filename, file.content_type)

    # Read file bytes with size limit
    file_bytes = await file.read()
    if len(file_bytes) > _MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Max size: {_MAX_FILE_SIZE_BYTES // 1024 // 1024}MB",
        )

    # Create ingestion service
    llm_client = LLMClient(settings)
    service = IngestionService(db=db, settings=settings, llm_client=llm_client)

    # Create job
    job = await service.create_job(
        tenant_id=current_user.tenant_id,
        filename=filename,
        file_type=file_type,
        file_size=len(file_bytes),
    )

    audit = AuditService(db)

    # Process job synchronously for now; move to background task queue for high throughput
    try:
        job = await service.process_job(job_id=job.id, file_bytes=file_bytes)

        await audit.log(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="document.ingestion.complete",
            resource_type="ingestion_job",
            resource_id=str(job.id),
            status=AuditStatus.SUCCESS,
            extra={"filename": filename, "chunk_count": job.chunk_count},
        )

    except Exception as exc:
        await audit.log(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="document.ingestion.failed",
            resource_type="ingestion_job",
            resource_id=str(job.id),
            status=AuditStatus.ERROR,
            error_detail=str(exc),
        )
        # Job is in FAILED state - return it (don't raise)
        # Client can check status and retry

    return _job_to_response(job)


@router.get(
    "/jobs",
    response_model=IngestionJobListResponse,
    summary="List ingestion jobs for current tenant",
)
async def list_jobs(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    status_filter: IngestionStatus | None = Query(
        default=None,
        alias="status",
        description="Filter by job status",
    ),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
) -> IngestionJobListResponse:
    """List all ingestion jobs for the current tenant.

    Optionally filter by status. Results are ordered by creation time (newest first).
    """
    check_permission(current_user.role, Permission.DOCUMENT_READ)

    llm_client = LLMClient(settings)
    service = IngestionService(db=db, settings=settings, llm_client=llm_client)

    jobs = await service.list_jobs(
        tenant_id=current_user.tenant_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )

    return IngestionJobListResponse(
        jobs=[_job_to_response(j) for j in jobs],
        total=len(jobs),
    )


@router.get(
    "/jobs/{job_id}",
    response_model=IngestionJobResponse,
    summary="Get ingestion job status",
)
async def get_job(
    job_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestionJobResponse:
    """Get details and status of a specific ingestion job."""
    check_permission(current_user.role, Permission.DOCUMENT_READ)

    llm_client = LLMClient(settings)
    service = IngestionService(db=db, settings=settings, llm_client=llm_client)

    job = await service.get_job(job_id=job_id, tenant_id=current_user.tenant_id)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ingestion job not found",
        )

    return _job_to_response(job)


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=IngestionJobResponse,
    summary="Cancel an ingestion job",
)
async def cancel_job(
    job_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> IngestionJobResponse:
    """Cancel a pending or processing ingestion job.

    Only PENDING and PROCESSING jobs can be cancelled.
    COMPLETED and FAILED jobs cannot be cancelled.
    """
    check_permission(current_user.role, Permission.DOCUMENT_UPLOAD)

    llm_client = LLMClient(settings)
    service = IngestionService(db=db, settings=settings, llm_client=llm_client)

    try:
        job = await service.cancel_job(
            job_id=job_id,
            tenant_id=current_user.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return _job_to_response(job)


@router.get(
    "/jobs/{job_id}/chunks",
    response_model=list[ChunkResponse],
    summary="Get chunks from a completed ingestion job",
)
async def get_job_chunks(
    job_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    limit: int = Query(default=100, le=1000),
) -> list[ChunkResponse]:
    """Get the document chunks created by an ingestion job.

    Only available for COMPLETED jobs. Returns chunks with their
    content and metadata (but NOT embeddings for size reasons).
    """
    check_permission(current_user.role, Permission.DOCUMENT_READ)

    llm_client = LLMClient(settings)
    service = IngestionService(db=db, settings=settings, llm_client=llm_client)

    # Verify job exists and is completed
    job = await service.get_job(job_id=job_id, tenant_id=current_user.tenant_id)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ingestion job not found",
        )

    if job.status != IngestionStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job is in {job.status} status. Chunks are only available for COMPLETED jobs.",
        )

    chunks = await service.get_job_chunks(
        job_id=job_id,
        tenant_id=current_user.tenant_id,
        limit=limit,
    )

    return [
        ChunkResponse(
            id=chunk.id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            token_count=chunk.chunk_metadata.get("token_count"),
            metadata={
                k: v
                for k, v in chunk.chunk_metadata.items()
                if k != "embedding"  # Never return embeddings via API
            },
        )
        for chunk in chunks
    ]
