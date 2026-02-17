"""Playground API endpoints for agent debugging and testing.

POST /api/v1/playground/run         - Run an agent with a prompt
GET  /api/v1/playground/traces      - List execution traces
GET  /api/v1/playground/traces/{id} - Get trace with all steps
GET  /api/v1/playground/traces/{id}/stream - SSE stream for live updates
GET  /api/v1/playground/stats       - Aggregate statistics
POST /api/v1/playground/compare     - Run same prompt against 2 configs

All endpoints require authentication and are tenant-scoped.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient
from src.agent.orchestrator import AgentOrchestrator
from src.agent.tools import ToolGateway
from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.config import Settings, get_settings
from src.core.policy import Permission, check_permission
from src.database import get_db_session
from src.models.trace import StepType, TraceStatus
from src.services.tracing import TracingService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/playground", tags=["playground"])


# ------------------------------------------------------------------ #
# Request/Response Models
# ------------------------------------------------------------------ #


class PlaygroundRunRequest(BaseModel):
    """Request to run an agent in the playground."""
    prompt: str = Field(..., min_length=1, max_length=10_000)
    agent_spec_id: str | None = Field(
        default=None,
        description="Optional agent spec ID to use (otherwise auto-select)"
    )
    model_override: str | None = Field(
        default=None,
        description="Optional model override"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional metadata to store with trace"
    )


class PlaygroundRunResponse(BaseModel):
    """Response from running an agent."""
    trace_id: uuid.UUID
    response: str
    agent_spec_id: str
    total_steps: int
    total_tokens: int
    duration_ms: int


class TraceStepResponse(BaseModel):
    """Response model for a trace step."""
    id: uuid.UUID
    step_number: int
    step_type: str
    input_data: dict[str, Any]
    output_data: dict[str, Any]
    started_at: datetime
    completed_at: datetime | None
    token_count: int
    model_used: str | None
    metadata: dict[str, Any]


class TraceResponse(BaseModel):
    """Response model for a trace."""
    id: uuid.UUID
    agent_spec_id: str
    started_at: datetime
    completed_at: datetime | None
    status: str
    error_message: str | None
    total_tokens: int
    total_steps: int
    input_message: str | None
    output_response: str | None
    metadata: dict[str, Any]
    steps: list[TraceStepResponse] = []


class TraceListResponse(BaseModel):
    """Response model for listing traces (without steps)."""
    id: uuid.UUID
    agent_spec_id: str
    started_at: datetime
    completed_at: datetime | None
    status: str
    total_tokens: int
    total_steps: int
    input_message: str | None


class TraceStatsResponse(BaseModel):
    """Response model for trace statistics."""
    total_traces: int
    success_rate: float
    avg_tokens: int
    avg_steps: int
    avg_duration_ms: int


class CompareRequest(BaseModel):
    """Request to compare two agent configurations."""
    prompt: str = Field(..., min_length=1, max_length=10_000)
    config_a: dict[str, Any] = Field(..., description="First agent config")
    config_b: dict[str, Any] = Field(..., description="Second agent config")


class CompareResponse(BaseModel):
    """Response from comparing two agent runs."""
    trace_a_id: uuid.UUID
    trace_b_id: uuid.UUID
    response_a: str
    response_b: str
    duration_a_ms: int
    duration_b_ms: int
    tokens_a: int
    tokens_b: int


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #


@router.post(
    "/run",
    response_model=PlaygroundRunResponse,
    summary="Run an agent with a prompt",
    description=(
        "Execute an agent in the playground with tracing enabled. "
        "Returns the trace ID and response. Use /traces/{id} to get full details."
    ),
)
async def run_agent(
    body: PlaygroundRunRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> PlaygroundRunResponse:
    """Run an agent in the playground with tracing."""
    # Permission check - operator or admin only
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    tracing = TracingService(db)

    # Start trace
    trace = await tracing.start_trace(
        tenant_id=current_user.tenant_id,
        agent_spec_id=body.agent_spec_id or "auto_select",
        input_message=body.prompt,
        metadata=body.metadata,
    )

    try:
        # Create orchestrator
        orchestrator = AgentOrchestrator(
            db=db,
            settings=settings,
            llm_client=LLMClient(settings),
            tool_gateway=ToolGateway(),
        )

        # Execute agent with tracing
        import time
        start = time.perf_counter()

        result = await orchestrator.route(
            user=current_user.user,
            message=body.prompt,
            conversation_id=None,
            rag_context="",
            conversation_history=[],
            citations=[],
        )

        duration_ms = int((time.perf_counter() - start) * 1000)

        # Record orchestrator steps as trace steps
        for idx, step_trace in enumerate(result.reasoning_trace):
            await tracing.add_step(
                trace_id=trace.id,
                step_type=StepType.REASONING,
                input_data={"step": step_trace},
                output_data={"description": step_trace},
                token_count=0,
                metadata={},
            )

        # Complete trace
        await tracing.complete_trace(
            trace_id=trace.id,
            status=TraceStatus.COMPLETED,
            output_response=result.response,
        )

        await db.commit()

        return PlaygroundRunResponse(
            trace_id=trace.id,
            response=result.response,
            agent_spec_id=result.agent_id,
            total_steps=trace.total_steps,
            total_tokens=trace.total_tokens,
            duration_ms=duration_ms,
        )

    except Exception as exc:
        log.error("playground.run_failed", error=str(exc), exc_info=True)

        # Mark trace as failed
        await tracing.complete_trace(
            trace_id=trace.id,
            status=TraceStatus.FAILED,
            error_message=str(exc),
        )

        await db.commit()

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent execution failed: {str(exc)}",
        ) from exc


@router.get(
    "/traces",
    response_model=list[TraceListResponse],
    summary="List execution traces",
    description="Get a list of recent agent execution traces for debugging.",
)
async def list_traces(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    status: TraceStatus | None = Query(default=None),
    agent_spec_id: str | None = Query(default=None),
) -> list[TraceListResponse]:
    """List traces for the current tenant."""
    # Permission check - operator or admin only
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    tracing = TracingService(db)

    traces = await tracing.list_traces(
        tenant_id=current_user.tenant_id,
        limit=limit,
        offset=offset,
        status=status,
        agent_spec_id=agent_spec_id,
    )

    return [
        TraceListResponse(
            id=t.id,
            agent_spec_id=t.agent_spec_id,
            started_at=t.started_at,
            completed_at=t.completed_at,
            status=t.status.value,
            total_tokens=t.total_tokens,
            total_steps=t.total_steps,
            input_message=t.input_message,
        )
        for t in traces
    ]


@router.get(
    "/traces/{trace_id}",
    response_model=TraceResponse,
    summary="Get trace with all steps",
    description=(
        "Get a complete trace including all reasoning steps, tool calls, "
        "and LLM interactions for visualization."
    ),
)
async def get_trace(
    trace_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TraceResponse:
    """Get a trace with all steps."""
    # Permission check - operator or admin only
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    tracing = TracingService(db)

    trace = await tracing.get_trace(
        trace_id=trace_id,
        tenant_id=current_user.tenant_id,
    )

    if not trace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trace not found",
        )

    return TraceResponse(
        id=trace.id,
        agent_spec_id=trace.agent_spec_id,
        started_at=trace.started_at,
        completed_at=trace.completed_at,
        status=trace.status.value,
        error_message=trace.error_message,
        total_tokens=trace.total_tokens,
        total_steps=trace.total_steps,
        input_message=trace.input_message,
        output_response=trace.output_response,
        metadata=trace.metadata_,
        steps=[
            TraceStepResponse(
                id=step.id,
                step_number=step.step_number,
                step_type=step.step_type.value,
                input_data=step.input_data,
                output_data=step.output_data,
                started_at=step.started_at,
                completed_at=step.completed_at,
                token_count=step.token_count,
                model_used=step.model_used,
                metadata=step.metadata_,
            )
            for step in trace.steps
        ],
    )


@router.get(
    "/traces/{trace_id}/stream",
    summary="Stream trace updates (SSE)",
    description=(
        "Get real-time updates for a running trace using Server-Sent Events. "
        "Use this for live debugging of agent execution."
    ),
)
async def stream_trace(
    trace_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """Stream trace updates using SSE."""
    # Permission check - operator or admin only
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    async def event_generator() -> AsyncIterator[str]:
        """Generate SSE events for trace updates."""
        tracing = TracingService(db)

        # Poll for updates (in production, use websockets or pub/sub)
        while True:
            trace = await tracing.get_trace(
                trace_id=trace_id,
                tenant_id=current_user.tenant_id,
            )

            if not trace:
                yield "event: error\ndata: {\"message\": \"Trace not found\"}\n\n"
                break

            # Send current state
            yield f"event: update\ndata: {{\"status\": \"{trace.status}\", \"steps\": {trace.total_steps}}}\n\n"

            # Stop streaming when trace is complete
            if trace.status in [TraceStatus.COMPLETED, TraceStatus.FAILED, TraceStatus.CANCELLED]:
                yield f"event: done\ndata: {{\"trace_id\": \"{trace.id}\"}}\n\n"
                break

            # Wait before next poll
            import asyncio
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/stats",
    response_model=TraceStatsResponse,
    summary="Get aggregate statistics",
    description="Get aggregate statistics for all traces in the tenant.",
)
async def get_stats(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TraceStatsResponse:
    """Get aggregate trace statistics."""
    # Permission check - operator or admin only
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    tracing = TracingService(db)

    stats = await tracing.get_trace_stats(tenant_id=current_user.tenant_id)

    return TraceStatsResponse(**stats)


@router.post(
    "/compare",
    response_model=CompareResponse,
    summary="Compare two agent configurations",
    description=(
        "Run the same prompt against two different agent configurations "
        "and return both traces for comparison."
    ),
)
async def compare_agents(
    body: CompareRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> CompareResponse:
    """Run same prompt against two agent configs and compare."""
    # Permission check - operator or admin only
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    tracing = TracingService(db)

    # Run config A
    trace_a = await tracing.start_trace(
        tenant_id=current_user.tenant_id,
        agent_spec_id=body.config_a.get("agent_spec_id", "auto_select"),
        input_message=body.prompt,
        metadata={"config": "A", **body.config_a},
    )

    # Run config B
    trace_b = await tracing.start_trace(
        tenant_id=current_user.tenant_id,
        agent_spec_id=body.config_b.get("agent_spec_id", "auto_select"),
        input_message=body.prompt,
        metadata={"config": "B", **body.config_b},
    )

    try:
        orchestrator = AgentOrchestrator(
            db=db,
            settings=settings,
            llm_client=LLMClient(settings),
            tool_gateway=ToolGateway(),
        )

        # Execute config A
        import time
        start_a = time.perf_counter()

        result_a = await orchestrator.route(
            user=current_user.user,
            message=body.prompt,
            conversation_id=None,
            rag_context="",
            conversation_history=[],
            citations=[],
        )

        duration_a_ms = int((time.perf_counter() - start_a) * 1000)

        await tracing.complete_trace(
            trace_id=trace_a.id,
            status=TraceStatus.COMPLETED,
            output_response=result_a.response,
        )

        # Execute config B
        start_b = time.perf_counter()

        result_b = await orchestrator.route(
            user=current_user.user,
            message=body.prompt,
            conversation_id=None,
            rag_context="",
            conversation_history=[],
            citations=[],
        )

        duration_b_ms = int((time.perf_counter() - start_b) * 1000)

        await tracing.complete_trace(
            trace_id=trace_b.id,
            status=TraceStatus.COMPLETED,
            output_response=result_b.response,
        )

        await db.commit()

        return CompareResponse(
            trace_a_id=trace_a.id,
            trace_b_id=trace_b.id,
            response_a=result_a.response,
            response_b=result_b.response,
            duration_a_ms=duration_a_ms,
            duration_b_ms=duration_b_ms,
            tokens_a=trace_a.total_tokens,
            tokens_b=trace_b.total_tokens,
        )

    except Exception as exc:
        log.error("playground.compare_failed", error=str(exc), exc_info=True)

        # Mark both traces as failed
        await tracing.complete_trace(
            trace_id=trace_a.id,
            status=TraceStatus.FAILED,
            error_message=str(exc),
        )
        await tracing.complete_trace(
            trace_id=trace_b.id,
            status=TraceStatus.FAILED,
            error_message=str(exc),
        )

        await db.commit()

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Comparison failed: {str(exc)}",
        ) from exc
