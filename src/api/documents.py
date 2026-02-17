"""Document management endpoints.

POST /documents/upload    - Upload a document (operator+)
GET  /documents           - List tenant documents
GET  /documents/{id}      - Get document details
DELETE /documents/{id}    - Delete document (admin only)

All endpoints are scoped to the authenticated user's tenant. Cross-tenant
access is impossible through this API.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient
from src.auth.api_key_auth import require_scope
from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.config import Settings, get_settings
from src.core.audit import AuditService
from src.core.policy import Permission, apply_tenant_filter, check_permission
from src.database import get_db_session
from src.models.audit import AuditStatus
from src.models.document import Document, DocumentStatus
from src.rag.ingest import IngestionPipeline

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"], dependencies=[Depends(require_scope("documents"))])

_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
}
_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB


class DocumentResponse(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    version: str
    status: str
    chunk_count: int
    metadata: dict[str, Any]

    model_config = {"from_attributes": True}


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]
    total: int


@router.post(
    "/upload",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document for ingestion",
)
async def upload_document(
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> DocumentResponse:
    """Upload a document. Triggers async ingestion (chunk + embed + store)."""
    check_permission(current_user.role, Permission.DOCUMENT_UPLOAD)

    # Validate content type
    ct = file.content_type or "application/octet-stream"
    if ct not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Content type {ct!r} not supported. Allowed: {sorted(_ALLOWED_CONTENT_TYPES)}",
        )

    # Read file (with size limit)
    file_bytes = await file.read()
    if len(file_bytes) > _MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Max size: {_MAX_FILE_SIZE_BYTES // 1024 // 1024} MB",
        )

    # Create Document record
    document = Document(
        tenant_id=current_user.tenant_id,
        uploaded_by_user_id=current_user.id,
        filename=file.filename or "unknown",
        content_type=ct,
        size_bytes=len(file_bytes),
        status=DocumentStatus.PENDING,
    )
    db.add(document)
    await db.flush()

    audit = AuditService(db)

    # Run ingestion synchronously for now; move to background task queue for high throughput
    try:
        pipeline = IngestionPipeline(db=db, settings=settings, llm_client=LLMClient(settings))
        chunk_count = await pipeline.ingest_document(
            document=document,
            file_bytes=file_bytes,
            content_type=ct,
        )
        await audit.log(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="document.upload",
            resource_type="document",
            resource_id=str(document.id),
            status=AuditStatus.SUCCESS,
            extra={"filename": document.filename, "chunk_count": chunk_count},
        )
    except Exception as exc:
        await audit.log(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="document.upload",
            resource_type="document",
            resource_id=str(document.id),
            status=AuditStatus.ERROR,
            error_detail=str(exc),
        )
        # Don't re-raise - document is in FAILED state, user can retry

    return DocumentResponse(
        id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        version=document.version,
        status=document.status.value,
        chunk_count=document.chunk_count,
        metadata=document.metadata_,
    )


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List all documents for the current tenant",
)
async def list_documents(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    limit: int = 50,
    offset: int = 0,
) -> DocumentListResponse:
    """List all documents in the current tenant."""
    check_permission(current_user.role, Permission.DOCUMENT_READ)

    stmt = apply_tenant_filter(
        select(Document).order_by(Document.created_at.desc()).offset(offset).limit(limit),
        Document,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    docs = result.scalars().all()

    return DocumentListResponse(
        documents=[
            DocumentResponse(
                id=d.id,
                filename=d.filename,
                content_type=d.content_type,
                size_bytes=d.size_bytes,
                version=d.version,
                status=d.status.value,
                chunk_count=d.chunk_count,
                metadata=d.metadata_,
            )
            for d in docs
        ],
        total=len(docs),
    )


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Get document details",
)
async def get_document(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> DocumentResponse:
    """Get a single document by ID (tenant-scoped)."""
    check_permission(current_user.role, Permission.DOCUMENT_READ)

    stmt = apply_tenant_filter(
        select(Document).where(Document.id == document_id),
        Document,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    doc = result.scalar_one_or_none()

    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    return DocumentResponse(
        id=doc.id,
        filename=doc.filename,
        content_type=doc.content_type,
        size_bytes=doc.size_bytes,
        version=doc.version,
        status=doc.status.value,
        chunk_count=doc.chunk_count,
        metadata=doc.metadata_,
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document (admin only)",
)
async def delete_document(
    document_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a document and all its chunks (admin only)."""
    check_permission(current_user.role, Permission.DOCUMENT_DELETE)

    stmt = apply_tenant_filter(
        select(Document).where(Document.id == document_id),
        Document,
        current_user.tenant_id,
    )
    result = await db.execute(stmt)
    doc = result.scalar_one_or_none()

    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    await db.delete(doc)

    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="document.delete",
        resource_type="document",
        resource_id=str(document_id),
        status=AuditStatus.SUCCESS,
    )
