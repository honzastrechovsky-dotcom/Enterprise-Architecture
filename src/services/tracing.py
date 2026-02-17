"""Agent tracing service for playground debugging and observability.

The TracingService provides methods to:
1. Create and manage agent execution traces
2. Record individual reasoning steps
3. Query traces for debugging and comparison
4. Calculate aggregate statistics

All operations are tenant-scoped for multi-tenancy isolation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.policy import apply_tenant_filter
from src.models.trace import AgentStep, AgentTrace, StepType, TraceStatus

log = structlog.get_logger(__name__)


class TracingService:
    """Service for managing agent execution traces.

    Provides CRUD operations for traces and steps with automatic tenant scoping.
    Used by the agent orchestrator to record execution details and by the
    playground API to query and visualize traces.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the tracing service.

        Args:
            db: Database session for trace operations
        """
        self._db = db

    async def start_trace(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_spec_id: str,
        conversation_id: uuid.UUID | None = None,
        input_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentTrace:
        """Start a new agent execution trace.

        Creates a trace record in RUNNING status. The orchestrator should call
        this at the start of agent execution.

        Args:
            tenant_id: Tenant ID for isolation
            agent_spec_id: Agent specification ID (e.g., "qa_agent")
            conversation_id: Optional conversation ID if from chat
            input_message: Optional user input message
            metadata: Optional metadata (model config, settings, etc.)

        Returns:
            Created AgentTrace instance
        """
        trace = AgentTrace(
            tenant_id=tenant_id,
            agent_spec_id=agent_spec_id,
            conversation_id=conversation_id,
            input_message=input_message,
            status=TraceStatus.RUNNING,
            metadata_=metadata or {},
        )
        self._db.add(trace)
        await self._db.flush()

        log.info(
            "tracing.trace_started",
            trace_id=str(trace.id),
            tenant_id=str(tenant_id),
            agent_spec_id=agent_spec_id,
        )

        return trace

    async def add_step(
        self,
        *,
        trace_id: uuid.UUID,
        step_type: StepType,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        token_count: int = 0,
        model_used: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentStep:
        """Add a step to an existing trace.

        Records a single reasoning step, tool call, or LLM interaction.
        Steps are automatically numbered in sequence.

        Args:
            trace_id: Trace ID to add step to
            step_type: Type of step (observe, think, execute, etc.)
            input_data: Input data for this step
            output_data: Output data from this step
            token_count: Number of tokens used (if LLM call)
            model_used: Model name (if LLM call)
            metadata: Optional metadata (confidence, citations, etc.)

        Returns:
            Created AgentStep instance
        """
        # Get current step count for this trace
        stmt = (
            select(func.count(AgentStep.id))
            .where(AgentStep.trace_id == trace_id)
        )
        result = await self._db.execute(stmt)
        current_count = result.scalar() or 0

        step = AgentStep(
            trace_id=trace_id,
            step_number=current_count + 1,
            step_type=step_type,
            input_data=input_data,
            output_data=output_data,
            token_count=token_count,
            model_used=model_used,
            completed_at=datetime.now(UTC),
            metadata_=metadata or {},
        )
        self._db.add(step)
        await self._db.flush()

        # Update trace total_steps and total_tokens
        trace_stmt = select(AgentTrace).where(AgentTrace.id == trace_id)
        trace_result = await self._db.execute(trace_stmt)
        trace = trace_result.scalar_one()
        trace.total_steps += 1
        trace.total_tokens += token_count

        log.debug(
            "tracing.step_added",
            trace_id=str(trace_id),
            step_number=step.step_number,
            step_type=step_type,
            token_count=token_count,
        )

        return step

    async def complete_trace(
        self,
        *,
        trace_id: uuid.UUID,
        status: TraceStatus,
        error_message: str | None = None,
        output_response: str | None = None,
    ) -> None:
        """Complete an agent execution trace.

        Updates the trace status to completed/failed and records the completion
        time and final output.

        Args:
            trace_id: Trace ID to complete
            status: Final status (completed, failed, cancelled)
            error_message: Error message if failed
            output_response: Final agent response
        """
        stmt = select(AgentTrace).where(AgentTrace.id == trace_id)
        result = await self._db.execute(stmt)
        trace = result.scalar_one()

        trace.status = status
        trace.completed_at = datetime.now(UTC)
        trace.error_message = error_message
        trace.output_response = output_response

        await self._db.flush()

        log.info(
            "tracing.trace_completed",
            trace_id=str(trace_id),
            status=status,
            total_steps=trace.total_steps,
            total_tokens=trace.total_tokens,
        )

    async def get_trace(
        self,
        *,
        trace_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> AgentTrace | None:
        """Get a trace with all its steps.

        Loads the trace and eagerly loads all steps for visualization.

        Args:
            trace_id: Trace ID to fetch
            tenant_id: Tenant ID for isolation

        Returns:
            AgentTrace with steps loaded, or None if not found
        """
        stmt = apply_tenant_filter(
            select(AgentTrace).where(AgentTrace.id == trace_id),
            AgentTrace,
            tenant_id,
        )
        result = await self._db.execute(stmt)
        trace = result.scalar_one_or_none()

        if trace:
            # Eagerly load steps
            await self._db.refresh(trace, ["steps"])

        return trace

    async def list_traces(
        self,
        *,
        tenant_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        status: TraceStatus | None = None,
        agent_spec_id: str | None = None,
    ) -> list[AgentTrace]:
        """List traces for a tenant.

        Returns traces in reverse chronological order (newest first).

        Args:
            tenant_id: Tenant ID for isolation
            limit: Maximum number of traces to return
            offset: Offset for pagination
            status: Optional filter by status
            agent_spec_id: Optional filter by agent spec ID

        Returns:
            List of AgentTrace instances (without steps loaded)
        """
        stmt = apply_tenant_filter(
            select(AgentTrace).order_by(AgentTrace.started_at.desc()),
            AgentTrace,
            tenant_id,
        )

        if status:
            stmt = stmt.where(AgentTrace.status == status)

        if agent_spec_id:
            stmt = stmt.where(AgentTrace.agent_spec_id == agent_spec_id)

        stmt = stmt.limit(limit).offset(offset)

        result = await self._db.execute(stmt)
        traces = list(result.scalars().all())

        log.debug(
            "tracing.list_traces",
            tenant_id=str(tenant_id),
            count=len(traces),
            filters={"status": status, "agent_spec_id": agent_spec_id},
        )

        return traces

    async def get_trace_stats(
        self,
        *,
        tenant_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Get aggregate statistics for traces in a tenant.

        Calculates:
        - Total traces
        - Success rate (completed / total)
        - Average tokens per trace
        - Average steps per trace
        - Average duration

        Args:
            tenant_id: Tenant ID for isolation

        Returns:
            Dictionary with aggregate statistics
        """
        # Get all completed traces for stats
        stmt = apply_tenant_filter(
            select(AgentTrace).where(
                AgentTrace.status.in_([TraceStatus.COMPLETED, TraceStatus.FAILED])
            ),
            AgentTrace,
            tenant_id,
        )
        result = await self._db.execute(stmt)
        traces = list(result.scalars().all())

        if not traces:
            return {
                "total_traces": 0,
                "success_rate": 0.0,
                "avg_tokens": 0,
                "avg_steps": 0,
                "avg_duration_ms": 0,
            }

        completed_count = sum(1 for t in traces if t.status == TraceStatus.COMPLETED)
        total_tokens = sum(t.total_tokens for t in traces)
        total_steps = sum(t.total_steps for t in traces)

        # Calculate average duration for completed traces
        durations = []
        for trace in traces:
            if trace.completed_at and trace.started_at:
                duration = (trace.completed_at - trace.started_at).total_seconds() * 1000
                durations.append(duration)

        avg_duration_ms = int(sum(durations) / len(durations)) if durations else 0

        stats = {
            "total_traces": len(traces),
            "success_rate": completed_count / len(traces) if traces else 0.0,
            "avg_tokens": int(total_tokens / len(traces)),
            "avg_steps": int(total_steps / len(traces)),
            "avg_duration_ms": avg_duration_ms,
        }

        log.debug("tracing.stats_calculated", tenant_id=str(tenant_id), stats=stats)

        return stats
