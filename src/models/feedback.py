"""Feedback models for user response ratings and fine-tuning data collection.

These models support the feedback loop system that collects user ratings
(thumbs up/down, 1-5 scale) on agent responses for quality monitoring and
fine-tuning data preparation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class FeedbackRating(StrEnum):
    """Feedback rating types."""
    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"
    RATING_1 = "rating_1"
    RATING_2 = "rating_2"
    RATING_3 = "rating_3"
    RATING_4 = "rating_4"
    RATING_5 = "rating_5"


class DatasetStatus(StrEnum):
    """Fine-tuning dataset status."""
    DRAFT = "draft"
    READY = "ready"
    EXPORTED = "exported"


class ResponseFeedback(Base):
    """User feedback on agent responses.

    Captures thumbs up/down or 1-5 ratings on agent responses along with
    optional comments and tags. Used for quality monitoring and as source
    data for fine-tuning datasets.
    """
    __tablename__ = "response_feedback"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Optional conversation/message linkage
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Optional trace ID for distributed tracing correlation
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # Rating and feedback
    rating: Mapped[FeedbackRating] = mapped_column(
        Enum(FeedbackRating, name="feedback_rating"),
        nullable=False,
        index=True,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="Tags like ['accurate', 'helpful', 'too_slow']",
    )

    # Captured context for fine-tuning
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant")  # type: ignore[name-defined]
    user: Mapped[User] = relationship("User")  # type: ignore[name-defined]
    conversation: Mapped[Conversation | None] = relationship("Conversation")  # type: ignore[name-defined]
    message: Mapped[Message | None] = relationship("Message")  # type: ignore[name-defined]

    __table_args__ = (
        Index("ix_feedback_tenant_rating", "tenant_id", "rating"),
        Index("ix_feedback_tenant_created", "tenant_id", "created_at"),
        Index("ix_feedback_tenant_model", "tenant_id", "model_used"),
    )

    def __repr__(self) -> str:
        return f"<ResponseFeedback id={self.id} rating={self.rating} tenant={self.tenant_id}>"


class FinetuningDataset(Base):
    """Fine-tuning dataset created from filtered feedback.

    Represents a curated collection of feedback records that can be
    exported in fine-tuning format (e.g., OpenAI JSONL).
    """
    __tablename__ = "finetuning_datasets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[DatasetStatus] = mapped_column(
        Enum(DatasetStatus, name="dataset_status"),
        nullable=False,
        default=DatasetStatus.DRAFT,
        index=True,
    )

    record_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of records in this dataset",
    )

    # Filters used to populate this dataset
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Filters: min_rating, tags, date_range, model",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant")  # type: ignore[name-defined]
    records: Mapped[list[FinetuningRecord]] = relationship(
        "FinetuningRecord",
        back_populates="dataset",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_datasets_tenant_status", "tenant_id", "status"),
        Index("ix_datasets_tenant_created", "tenant_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<FinetuningDataset id={self.id} name={self.name!r} status={self.status}>"


class FinetuningRecord(Base):
    """Individual record in a fine-tuning dataset.

    Each record represents a training example derived from feedback,
    formatted with system prompt, user prompt, and assistant response.
    """
    __tablename__ = "finetuning_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("finetuning_datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    feedback_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("response_feedback.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Fine-tuning format fields
    system_prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="You are a helpful assistant.",
    )
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_response: Mapped[str] = mapped_column(Text, nullable=False)

    # Quality metrics
    quality_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        comment="Quality score (0.0-1.0) for weighting or filtering",
    )

    # Allow excluding records without deleting
    included: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether to include in exports",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # Relationships
    dataset: Mapped[FinetuningDataset] = relationship(
        "FinetuningDataset",
        back_populates="records",
    )
    feedback: Mapped[ResponseFeedback] = relationship("ResponseFeedback")

    __table_args__ = (
        Index("ix_records_dataset_included", "dataset_id", "included"),
        Index("ix_records_feedback", "feedback_id"),
    )

    def __repr__(self) -> str:
        return f"<FinetuningRecord id={self.id} dataset={self.dataset_id}>"
