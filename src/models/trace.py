"""Agent trace and step models for debugging and playground.

AgentTrace represents a single agent execution session with all its reasoning
steps, tool calls, and LLM interactions. Used by the playground UI to visualize
agent decision-making and by developers to debug agent behavior.

All traces are tenant-scoped for multi-tenancy isolation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
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


class TraceStatus(StrEnum):
    """Status of an agent trace execution."""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepType(StrEnum):
    """Type of reasoning step in agent execution."""
    OBSERVE = "observe"
    THINK = "think"
    PLAN = "plan"
    EXECUTE = "execute"
    VERIFY = "verify"
    TOOL_CALL = "tool_call"
    REASONING = "reasoning"
    LLM_CALL = "llm_call"


class AgentTrace(Base):
    """Trace of a complete agent execution session.

    Captures the full execution context, timing, and status of an agent run.
    Each trace contains multiple steps (AgentStep) representing the agent's
    reasoning chain, tool calls, and LLM interactions.
    """
    __tablename__ = "agent_traces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Optional: link to conversation if trace was from chat
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Agent spec ID (e.g., "qa_agent", "knowledge_expert")
    agent_spec_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )

    # Execution timing
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Execution status and outcome
    status: Mapped[TraceStatus] = mapped_column(
        Enum(TraceStatus, name="trace_status"),
        nullable=False,
        default=TraceStatus.RUNNING,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Aggregate metrics
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    total_steps: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Input/output for comparison
    input_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    output_response: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Additional metadata (model config, settings, etc.)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant")  # type: ignore[name-defined]
    conversation: Mapped[Conversation | None] = relationship("Conversation")  # type: ignore[name-defined]
    steps: Mapped[list[AgentStep]] = relationship(
        "AgentStep",
        back_populates="trace",
        cascade="all, delete-orphan",
        order_by="AgentStep.step_number",
    )

    __table_args__ = (
        Index("ix_agent_traces_tenant_started", "tenant_id", "started_at"),
        Index("ix_agent_traces_tenant_agent", "tenant_id", "agent_spec_id"),
        Index("ix_agent_traces_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<AgentTrace id={self.id} agent={self.agent_spec_id} status={self.status}>"


class AgentStep(Base):
    """Individual step in an agent execution trace.

    Represents a single reasoning step, tool call, or LLM interaction within
    an agent execution. Steps are ordered by step_number and capture the full
    input/output data and timing for debugging.
    """
    __tablename__ = "agent_steps"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    trace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_traces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Step ordering and type
    step_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    step_type: Mapped[StepType] = mapped_column(
        Enum(StepType, name="step_type"),
        nullable=False,
    )

    # Step data
    input_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    output_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    # Timing
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # LLM metrics (if applicable)
    token_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    model_used: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    # Additional metadata (confidence scores, citations, etc.)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    # Relationships
    trace: Mapped[AgentTrace] = relationship(
        "AgentTrace",
        back_populates="steps",
    )

    __table_args__ = (
        Index("ix_agent_steps_trace_step", "trace_id", "step_number"),
        Index("ix_agent_steps_type", "step_type"),
    )

    def __repr__(self) -> str:
        return f"<AgentStep id={self.id} trace={self.trace_id} step={self.step_number} type={self.step_type}>"
