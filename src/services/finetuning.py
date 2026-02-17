"""Fine-tuning dataset service for managing training data from feedback.

Provides methods to create, populate, and export fine-tuning datasets
derived from user feedback. All operations are tenant-scoped.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.feedback import (
    DatasetStatus,
    FeedbackRating,
    FinetuningDataset,
    FinetuningRecord,
    ResponseFeedback,
)

log = structlog.get_logger(__name__)


class FinetuningService:
    """Service for managing fine-tuning datasets from feedback."""

    def __init__(self, db: AsyncSession):
        """Initialize finetuning service with database session.

        Args:
            db: Async database session
        """
        self.db = db

    async def create_dataset(
        self,
        tenant_id: uuid.UUID,
        name: str,
        description: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Create a new fine-tuning dataset.

        Args:
            tenant_id: Tenant UUID
            name: Dataset name
            description: Optional description
            filters: Optional filters to apply when populating

        Returns:
            UUID of created dataset
        """
        dataset = FinetuningDataset(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=name,
            description=description,
            filters=filters or {},
            status=DatasetStatus.DRAFT,
        )

        self.db.add(dataset)
        await self.db.flush()

        log.info(
            "dataset_created",
            dataset_id=str(dataset.id),
            tenant_id=str(tenant_id),
            name=name,
            filters=filters,
        )

        return dataset.id

    async def populate_dataset(
        self,
        dataset_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> int:
        """Populate dataset with feedback matching its filters.

        Args:
            dataset_id: Dataset UUID
            tenant_id: Tenant UUID

        Returns:
            Number of records added
        """
        # Get dataset
        stmt = select(FinetuningDataset).where(
            and_(
                FinetuningDataset.id == dataset_id,
                FinetuningDataset.tenant_id == tenant_id,
            )
        )
        result = await self.db.execute(stmt)
        dataset = result.scalar_one_or_none()

        if not dataset:
            return 0

        # Build feedback query based on filters
        feedback_stmt = select(ResponseFeedback).where(
            ResponseFeedback.tenant_id == tenant_id
        )

        filters = dataset.filters or {}

        # Apply min_rating filter
        if "min_rating" in filters:
            min_rating = filters["min_rating"]
            if min_rating == "thumbs_up":
                # Only thumbs_up or ratings 4-5
                positive_ratings = [
                    FeedbackRating.THUMBS_UP,
                    FeedbackRating.RATING_4,
                    FeedbackRating.RATING_5,
                ]
                feedback_stmt = feedback_stmt.where(
                    ResponseFeedback.rating.in_(positive_ratings)
                )

        # Apply tags filter (feedback must have all specified tags)
        if "tags" in filters and filters["tags"]:
            for tag in filters["tags"]:
                feedback_stmt = feedback_stmt.where(
                    ResponseFeedback.tags.op("@>")(json.dumps([tag]))
                )

        # Apply date range filter
        if "date_from" in filters:
            date_from = datetime.fromisoformat(filters["date_from"])
            feedback_stmt = feedback_stmt.where(
                ResponseFeedback.created_at >= date_from
            )
        if "date_to" in filters:
            date_to = datetime.fromisoformat(filters["date_to"])
            feedback_stmt = feedback_stmt.where(
                ResponseFeedback.created_at <= date_to
            )

        # Apply model filter
        if "model" in filters:
            feedback_stmt = feedback_stmt.where(
                ResponseFeedback.model_used == filters["model"]
            )

        # Execute query
        feedback_result = await self.db.execute(feedback_stmt)
        feedback_records = feedback_result.scalars().all()

        # Create finetuning records
        record_count = 0
        for feedback in feedback_records:
            # Check if already exists
            exists_stmt = select(FinetuningRecord).where(
                and_(
                    FinetuningRecord.dataset_id == dataset_id,
                    FinetuningRecord.feedback_id == feedback.id,
                )
            )
            exists_result = await self.db.execute(exists_stmt)
            if exists_result.scalar_one_or_none():
                continue  # Skip duplicates

            # Calculate quality score based on rating
            quality_score = self._calculate_quality_score(feedback.rating)

            record = FinetuningRecord(
                id=uuid.uuid4(),
                dataset_id=dataset_id,
                feedback_id=feedback.id,
                system_prompt="You are a helpful assistant.",
                user_prompt=feedback.prompt_text,
                assistant_response=feedback.response_text,
                quality_score=quality_score,
                included=True,
            )
            self.db.add(record)
            record_count += 1

        # Update dataset record count and status
        dataset.record_count = record_count
        dataset.status = DatasetStatus.READY if record_count > 0 else DatasetStatus.DRAFT

        await self.db.flush()

        log.info(
            "dataset_populated",
            dataset_id=str(dataset_id),
            tenant_id=str(tenant_id),
            record_count=record_count,
        )

        return record_count

    async def export_dataset(
        self,
        dataset_id: uuid.UUID,
        tenant_id: uuid.UUID,
        format: str = "openai",
    ) -> str:
        """Export dataset in specified fine-tuning format.

        Args:
            dataset_id: Dataset UUID
            tenant_id: Tenant UUID
            format: Export format (currently only "openai" supported)

        Returns:
            JSONL string with one training example per line
        """
        # Get dataset
        stmt = select(FinetuningDataset).where(
            and_(
                FinetuningDataset.id == dataset_id,
                FinetuningDataset.tenant_id == tenant_id,
            )
        )
        result = await self.db.execute(stmt)
        dataset = result.scalar_one_or_none()

        if not dataset:
            return ""

        # Get included records
        records_stmt = (
            select(FinetuningRecord)
            .where(
                and_(
                    FinetuningRecord.dataset_id == dataset_id,
                    FinetuningRecord.included == True,  # noqa: E712
                )
            )
            .order_by(FinetuningRecord.created_at)
        )
        records_result = await self.db.execute(records_stmt)
        records = records_result.scalars().all()

        # Export in OpenAI format
        lines = []
        for record in records:
            obj = {
                "messages": [
                    {"role": "system", "content": record.system_prompt},
                    {"role": "user", "content": record.user_prompt},
                    {"role": "assistant", "content": record.assistant_response},
                ]
            }
            lines.append(json.dumps(obj))

        # Mark as exported
        dataset.status = DatasetStatus.EXPORTED
        await self.db.flush()

        log.info(
            "dataset_exported",
            dataset_id=str(dataset_id),
            tenant_id=str(tenant_id),
            record_count=len(records),
            format=format,
        )

        return "\n".join(lines) + "\n" if lines else ""

    async def list_datasets(self, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
        """List all fine-tuning datasets for a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            List of dataset dictionaries
        """
        stmt = (
            select(FinetuningDataset)
            .where(FinetuningDataset.tenant_id == tenant_id)
            .order_by(desc(FinetuningDataset.created_at))
        )
        result = await self.db.execute(stmt)
        datasets = result.scalars().all()

        return [
            {
                "id": d.id,
                "name": d.name,
                "description": d.description,
                "status": d.status.value,
                "record_count": d.record_count,
                "filters": d.filters,
                "created_at": d.created_at,
                "updated_at": d.updated_at,
            }
            for d in datasets
        ]

    async def get_dataset(
        self,
        dataset_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Get dataset details with sample records.

        Args:
            dataset_id: Dataset UUID
            tenant_id: Tenant UUID

        Returns:
            Dataset dictionary with sample_records, or None if not found
        """
        # Get dataset
        stmt = select(FinetuningDataset).where(
            and_(
                FinetuningDataset.id == dataset_id,
                FinetuningDataset.tenant_id == tenant_id,
            )
        )
        result = await self.db.execute(stmt)
        dataset = result.scalar_one_or_none()

        if not dataset:
            return None

        # Get sample records (first 5)
        records_stmt = (
            select(FinetuningRecord)
            .where(FinetuningRecord.dataset_id == dataset_id)
            .limit(5)
        )
        records_result = await self.db.execute(records_stmt)
        records = records_result.scalars().all()

        sample_records = [
            {
                "system_prompt": r.system_prompt,
                "user_prompt": r.user_prompt,
                "assistant_response": r.assistant_response,
                "quality_score": r.quality_score,
                "included": r.included,
            }
            for r in records
        ]

        return {
            "id": dataset.id,
            "name": dataset.name,
            "description": dataset.description,
            "status": dataset.status.value,
            "record_count": dataset.record_count,
            "filters": dataset.filters,
            "created_at": dataset.created_at,
            "updated_at": dataset.updated_at,
            "sample_records": sample_records,
        }

    def _calculate_quality_score(self, rating: FeedbackRating) -> float:
        """Calculate quality score from rating.

        Args:
            rating: Feedback rating

        Returns:
            Quality score between 0.0 and 1.0
        """
        score_map = {
            FeedbackRating.THUMBS_UP: 1.0,
            FeedbackRating.THUMBS_DOWN: 0.0,
            FeedbackRating.RATING_5: 1.0,
            FeedbackRating.RATING_4: 0.8,
            FeedbackRating.RATING_3: 0.6,
            FeedbackRating.RATING_2: 0.4,
            FeedbackRating.RATING_1: 0.2,
        }
        return score_map.get(rating, 0.5)
