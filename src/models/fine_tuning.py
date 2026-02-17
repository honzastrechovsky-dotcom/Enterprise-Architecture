"""Fine-tuning job persistence model.

Stores fine-tuning jobs in PostgreSQL so they survive restarts and support
multi-instance deployments.  Each row represents one LoRA training job
from creation through completion or failure.

All reads MUST use apply_tenant_filter() — tenant_id is the isolation boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Float, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class FineTuningJobRecord(Base):
    """Persisted fine-tuning job with full training lifecycle.

    Lifecycle::

        pending -> preparing -> training -> evaluating -> completed
                                                       -> failed
        (any non-terminal state) -> cancelled

    The ``metrics`` column stores training-time metrics (loss curve, etc.)
    as they accumulate.  Final evaluation results are stored in
    ``evaluation_json`` once evaluate_model() is called.

    Token counts are derived from the actual dataset at preparation time,
    not hardcoded.
    """

    __tablename__ = "fine_tuning_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Fine-tuning job primary key",
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="Owning tenant — all queries must filter on this column",
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="User who created the job",
    )

    # Model and path configuration
    base_model: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Base model identifier in LiteLLM format (e.g. ollama/qwen2.5:7b)",
    )

    dataset_path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Filesystem path to training dataset JSONL",
    )

    output_path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Filesystem path where fine-tuned model/adapter will be saved",
    )

    # Training hyperparameters stored as JSON for forward compatibility
    hyperparameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment=(
            "Training hyperparameters: epochs, learning_rate, lora_rank, "
            "lora_alpha, batch_size, max_seq_length"
        ),
    )

    # Lifecycle status stored as VARCHAR (avoids enum migration pain)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="pending | preparing | training | evaluating | completed | failed | cancelled",
    )

    # Progress tracking
    progress: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default="0.0",
        comment="Training progress percentage (0-100)",
    )

    # Accumulates training metrics (loss, val_loss, etc.)
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Training metrics accumulated during the run",
    )

    # Final evaluation results (null until evaluate_model() is called)
    evaluation_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Evaluation results from evaluate_model(); null until evaluated",
    )

    # Error detail when status='failed'
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable error description when status=failed",
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="UTC timestamp of job creation",
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp when training preparation began",
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp when the job reached a terminal state",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        comment="UTC timestamp of last modification",
    )

    __table_args__ = (
        # Primary list query: all jobs for a tenant ordered by recency
        Index("ix_fine_tuning_jobs_tenant_created", "tenant_id", "created_at"),
        # Status filtering: list jobs by status within a tenant
        Index("ix_fine_tuning_jobs_tenant_status", "tenant_id", "status"),
    )

    def __init__(self, **kwargs: Any) -> None:
        """Initialize with Python-level defaults.

        SQLAlchemy mapped_column(default=...) only fires on INSERT, not at
        Python __init__ time.  We set the Python defaults here so that new
        objects are usable immediately after construction, before any DB flush.
        """
        kwargs.setdefault("id", uuid.uuid4())
        kwargs.setdefault("status", "pending")
        kwargs.setdefault("progress", 0.0)
        kwargs.setdefault("metrics", {})
        kwargs.setdefault("hyperparameters", {})
        kwargs.setdefault("created_at", datetime.now(UTC))
        kwargs.setdefault("updated_at", datetime.now(UTC))
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        progress = self.progress if self.progress is not None else 0.0
        return (
            f"<FineTuningJobRecord id={self.id} "
            f"tenant={self.tenant_id} "
            f"model={self.base_model!r} "
            f"status={self.status!r} "
            f"progress={progress:.0f}%>"
        )
