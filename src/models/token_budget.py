"""Token budget and routing metrics ORM models.

Design principles:
- TokenBudgetRecord: Per-tenant daily/monthly token budget state. One row per tenant,
  updated in place with SELECT ... FOR UPDATE to avoid race conditions.
- TokenUsageRecord: Append-only log of every token usage event for auditability
  and savings calculation.
- RoutingDecisionRecord: Append-only log of every routing decision for analytics
  and quality tracking.

All tables use UUID primary keys consistent with the rest of the platform.
No foreign keys to tenants table — the budget and metrics subsystem is intentionally
decoupled to allow operation even when tenant records are unavailable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class TokenBudgetRecord(Base):
    """Per-tenant token budget state.

    One row per tenant. Updated in-place on every usage recording.
    Use SELECT ... FOR UPDATE when reading before writing to prevent
    lost-update races under concurrent requests.

    Attributes:
        id: UUID primary key
        tenant_id: UUID of the owning tenant (unique — one budget per tenant)
        daily_limit: Maximum tokens allowed per day
        monthly_limit: Maximum tokens allowed per month
        current_daily: Tokens consumed today
        current_monthly: Tokens consumed this month
        last_reset_date: ISO date string (YYYY-MM-DD) of last daily counter reset
        last_reset_month: ISO month string (YYYY-MM) of last monthly counter reset
        updated_at: Last modification timestamp (UTC)
    """

    __tablename__ = "token_budgets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="Tenant this budget belongs to (unique — one budget per tenant)",
    )
    daily_limit: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1_000_000,
        comment="Maximum tokens allowed per calendar day (UTC)",
    )
    monthly_limit: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=20_000_000,
        comment="Maximum tokens allowed per calendar month (UTC)",
    )
    current_daily: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Tokens consumed since last_reset_date",
    )
    current_monthly: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Tokens consumed since last_reset_month",
    )
    last_reset_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="YYYY-MM-DD of last daily reset",
    )
    last_reset_month: Mapped[str] = mapped_column(
        String(7),
        nullable=False,
        comment="YYYY-MM of last monthly reset",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        comment="UTC timestamp of last update",
    )

    __table_args__ = (
        # Unique index — one budget row per tenant. Used as the lookup key.
        Index("ix_token_budgets_tenant", "tenant_id", unique=True),
    )

    def __repr__(self) -> str:
        return (
            f"<TokenBudgetRecord tenant={self.tenant_id} "
            f"daily={self.current_daily}/{self.daily_limit} "
            f"monthly={self.current_monthly}/{self.monthly_limit}>"
        )


class TokenUsageRecord(Base):
    """Append-only log of token usage events.

    One row per usage recording. Enables per-tenant savings reports
    and fine-grained auditing without mutating any counters.

    Attributes:
        id: UUID primary key
        tenant_id: UUID of the owning tenant
        timestamp: UTC timestamp of the usage event
        model_tier: Model tier used (light / standard / heavy)
        input_tokens: Prompt tokens consumed
        output_tokens: Completion tokens consumed
        total_tokens: input_tokens + output_tokens
        complexity_score: Task complexity score that drove tier selection (0.0-1.0)
    """

    __tablename__ = "token_usage_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="Tenant this usage event belongs to",
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="UTC timestamp when the tokens were consumed",
    )
    model_tier: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Model tier used: light | standard | heavy",
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Prompt tokens for this request",
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Completion tokens for this request",
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="input_tokens + output_tokens",
    )
    complexity_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        comment="Estimated task complexity score (0.0-1.0) at time of routing",
    )

    __table_args__ = (
        # Primary query pattern: all usage for a tenant within a time window
        Index("ix_token_usage_tenant_time", "tenant_id", "timestamp"),
    )

    def __repr__(self) -> str:
        return (
            f"<TokenUsageRecord tenant={self.tenant_id} "
            f"tier={self.model_tier} tokens={self.total_tokens}>"
        )


class RoutingDecisionRecord(Base):
    """Append-only log of model routing decisions.

    One row per routed request. Captures both the decision inputs
    (complexity, task type) and measured outcomes (quality, latency,
    tokens) to enable data-driven routing optimisation.

    Attributes:
        id: UUID primary key
        tenant_id: UUID of the owning tenant
        timestamp: UTC timestamp of the routing decision
        task_type: Classifier category of the task (e.g. "reasoning", "search")
        selected_tier: Model tier chosen (light / standard / heavy)
        estimated_complexity: Complexity score used to drive selection (0.0-1.0)
        actual_quality: Measured quality of response (0.0-1.0, nullable until scored)
        tokens_used: Total tokens consumed by this request
        latency_ms: End-to-end response latency in milliseconds
        metadata_json: JSON-encoded routing metadata (arbitrary key/value pairs)
    """

    __tablename__ = "routing_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="Tenant this routing decision belongs to",
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="UTC timestamp of the routing decision",
    )
    task_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Task classifier category (e.g. reasoning, search, summarization)",
    )
    selected_tier: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Model tier selected: light | standard | heavy",
    )
    estimated_complexity: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Complexity score (0.0-1.0) used to select the tier",
    )
    actual_quality: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Measured response quality (0.0-1.0); NULL until scored",
    )
    tokens_used: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Total tokens consumed by this request",
    )
    latency_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        comment="End-to-end latency in milliseconds",
    )
    metadata_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON-encoded arbitrary routing metadata",
    )

    __table_args__ = (
        # Primary query pattern: all decisions for a tenant within a time window
        Index("ix_routing_decisions_tenant_time", "tenant_id", "timestamp"),
    )

    def __repr__(self) -> str:
        return (
            f"<RoutingDecisionRecord tenant={self.tenant_id} "
            f"tier={self.selected_tier} task={self.task_type}>"
        )
