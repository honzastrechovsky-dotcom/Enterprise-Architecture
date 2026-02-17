"""Feedback and fine-tuning API endpoints.

POST   /api/v1/feedback                        - Submit feedback
GET    /api/v1/feedback                        - List feedback
GET    /api/v1/feedback/stats                  - Feedback statistics
GET    /api/v1/feedback/export                 - Export feedback as JSONL
POST   /api/v1/finetuning/datasets             - Create dataset
GET    /api/v1/finetuning/datasets             - List datasets
GET    /api/v1/finetuning/datasets/{id}        - Get dataset details
POST   /api/v1/finetuning/datasets/{id}/export - Export dataset

All endpoints require authentication and are tenant-scoped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.policy import Permission, check_permission
from src.database import get_db_session
from src.services.feedback import FeedbackService
from src.services.finetuning import FinetuningService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["feedback"])


# ------------------------------------------------------------------ #
# Request/Response Models
# ------------------------------------------------------------------ #


class SubmitFeedbackRequest(BaseModel):
    """Request to submit feedback on an agent response."""

    rating: str = Field(
        ...,
        description="Rating: thumbs_up, thumbs_down, rating_1-5",
        examples=["thumbs_up", "rating_4"],
    )
    prompt_text: str = Field(..., description="User's original prompt")
    response_text: str = Field(..., description="Agent's response")
    model_used: str = Field(..., description="Model identifier")
    comment: str | None = Field(None, description="Optional comment")
    tags: list[str] | None = Field(None, description="Optional tags", examples=[["accurate", "helpful"]])
    conversation_id: uuid.UUID | None = Field(None, description="Optional conversation ID")
    message_id: uuid.UUID | None = Field(None, description="Optional message ID")
    trace_id: str | None = Field(None, description="Optional trace ID")


class SubmitFeedbackResponse(BaseModel):
    """Response after submitting feedback."""

    id: uuid.UUID
    rating: str
    created_at: datetime


class FeedbackItem(BaseModel):
    """Single feedback item."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    trace_id: str | None
    rating: str
    comment: str | None
    tags: list[str]
    prompt_text: str
    response_text: str
    model_used: str
    created_at: datetime


class FeedbackStatsResponse(BaseModel):
    """Aggregated feedback statistics."""

    total_count: int
    positive_rate: float
    top_tags: list[dict[str, Any]]
    by_model: dict[str, Any]


class CreateDatasetRequest(BaseModel):
    """Request to create a fine-tuning dataset."""

    name: str = Field(..., description="Dataset name")
    description: str | None = Field(None, description="Optional description")
    filters: dict[str, Any] | None = Field(
        None,
        description="Filters: min_rating, tags, date_from, date_to, model",
        examples=[{"min_rating": "thumbs_up", "tags": ["accurate"]}],
    )


class CreateDatasetResponse(BaseModel):
    """Response after creating dataset."""

    id: uuid.UUID
    name: str
    status: str


class DatasetItem(BaseModel):
    """Single dataset item."""

    id: uuid.UUID
    name: str
    description: str | None
    status: str
    record_count: int
    filters: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DatasetDetailResponse(BaseModel):
    """Detailed dataset information with samples."""

    id: uuid.UUID
    name: str
    description: str | None
    status: str
    record_count: int
    filters: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    sample_records: list[dict[str, Any]]


class ExportDatasetRequest(BaseModel):
    """Request to export dataset."""

    format: str = Field(default="openai", description="Export format (openai)")


# ------------------------------------------------------------------ #
# Feedback Endpoints
# ------------------------------------------------------------------ #


@router.post(
    "/feedback",
    response_model=SubmitFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit feedback on an agent response",
)
async def submit_feedback(
    request: SubmitFeedbackRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> SubmitFeedbackResponse:
    """Submit user feedback on an agent response.

    Captures thumbs up/down or 1-5 rating along with optional comments
    and tags. Used for quality monitoring and fine-tuning data collection.
    """
    check_permission(current_user.role, Permission.FEEDBACK_SUBMIT)

    service = FeedbackService(db)
    feedback_id = await service.submit_feedback(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        rating=request.rating,
        prompt_text=request.prompt_text,
        response_text=request.response_text,
        model_used=request.model_used,
        comment=request.comment,
        tags=request.tags,
        conversation_id=request.conversation_id,
        message_id=request.message_id,
        trace_id=request.trace_id,
    )

    await db.commit()

    return SubmitFeedbackResponse(
        id=feedback_id,
        rating=request.rating,
        created_at=datetime.now(UTC),
    )


@router.get(
    "/feedback",
    response_model=list[FeedbackItem],
    summary="List feedback for the tenant",
)
async def list_feedback(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    limit: int = 50,
    offset: int = 0,
    rating: str | None = None,
) -> list[FeedbackItem]:
    """List feedback records for the tenant with optional filters."""
    check_permission(current_user.role, Permission.FEEDBACK_READ)

    service = FeedbackService(db)
    feedback_list = await service.list_feedback(
        tenant_id=current_user.tenant_id,
        limit=limit,
        offset=offset,
        rating=rating,
    )

    return [FeedbackItem(**item) for item in feedback_list]


@router.get(
    "/feedback/stats",
    response_model=FeedbackStatsResponse,
    summary="Get feedback statistics",
)
async def get_feedback_stats(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> FeedbackStatsResponse:
    """Get aggregated feedback statistics for the tenant."""
    check_permission(current_user.role, Permission.FEEDBACK_READ)

    service = FeedbackService(db)
    stats = await service.get_feedback_stats(tenant_id=current_user.tenant_id)

    return FeedbackStatsResponse(**stats)


@router.get(
    "/feedback/export",
    response_class=PlainTextResponse,
    summary="Export feedback as JSONL",
)
async def export_feedback(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    format: str = "jsonl",
    rating: str | None = None,
) -> PlainTextResponse:
    """Export feedback data in JSONL format for analysis or fine-tuning."""
    check_permission(current_user.role, Permission.FEEDBACK_EXPORT)

    service = FeedbackService(db)
    jsonl_data = await service.export_feedback(
        tenant_id=current_user.tenant_id,
        format=format,
        rating=rating,
    )

    return PlainTextResponse(
        content=jsonl_data,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename=feedback-{current_user.tenant_id}.jsonl"
        },
    )


# ------------------------------------------------------------------ #
# Fine-tuning Dataset Endpoints
# ------------------------------------------------------------------ #


@router.post(
    "/finetuning/datasets",
    response_model=CreateDatasetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a fine-tuning dataset",
)
async def create_finetuning_dataset(
    request: CreateDatasetRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> CreateDatasetResponse:
    """Create a new fine-tuning dataset from feedback.

    The dataset will be populated with feedback matching the specified filters.
    """
    check_permission(current_user.role, Permission.FINETUNING_MANAGE)

    service = FinetuningService(db)
    dataset_id = await service.create_dataset(
        tenant_id=current_user.tenant_id,
        name=request.name,
        description=request.description,
        filters=request.filters,
    )

    # Populate the dataset
    await service.populate_dataset(
        dataset_id=dataset_id,
        tenant_id=current_user.tenant_id,
    )

    await db.commit()

    return CreateDatasetResponse(
        id=dataset_id,
        name=request.name,
        status="ready",
    )


@router.get(
    "/finetuning/datasets",
    response_model=list[DatasetItem],
    summary="List fine-tuning datasets",
)
async def list_finetuning_datasets(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[DatasetItem]:
    """List all fine-tuning datasets for the tenant."""
    check_permission(current_user.role, Permission.FINETUNING_READ)

    service = FinetuningService(db)
    datasets = await service.list_datasets(tenant_id=current_user.tenant_id)

    return [DatasetItem(**item) for item in datasets]


@router.get(
    "/finetuning/datasets/{dataset_id}",
    response_model=DatasetDetailResponse,
    summary="Get dataset details",
)
async def get_finetuning_dataset(
    dataset_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> DatasetDetailResponse:
    """Get detailed information about a dataset including sample records."""
    check_permission(current_user.role, Permission.FINETUNING_READ)

    service = FinetuningService(db)
    dataset = await service.get_dataset(
        dataset_id=dataset_id,
        tenant_id=current_user.tenant_id,
    )

    if not dataset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dataset not found",
        )

    return DatasetDetailResponse(**dataset)


@router.post(
    "/finetuning/datasets/{dataset_id}/export",
    response_class=PlainTextResponse,
    summary="Export dataset in fine-tuning format",
)
async def export_finetuning_dataset(
    dataset_id: uuid.UUID,
    request: ExportDatasetRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PlainTextResponse:
    """Export dataset in OpenAI fine-tuning format (JSONL).

    Each line is a JSON object with a "messages" array containing
    system, user, and assistant messages.
    """
    check_permission(current_user.role, Permission.FINETUNING_EXPORT)

    service = FinetuningService(db)
    jsonl_data = await service.export_dataset(
        dataset_id=dataset_id,
        tenant_id=current_user.tenant_id,
        format=request.format,
    )

    if not jsonl_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dataset not found or empty",
        )

    await db.commit()

    return PlainTextResponse(
        content=jsonl_data,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename=dataset-{dataset_id}.jsonl"
        },
    )
