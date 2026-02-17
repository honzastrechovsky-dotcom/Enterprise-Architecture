"""API routes for goal planning and execution.

Endpoints:
- POST /api/v1/plans - Create execution plan from goal
- GET /api/v1/plans/{plan_id} - Get plan details
- POST /api/v1/plans/{plan_id}/approve - Approve plan for execution
- POST /api/v1/plans/{plan_id}/reject - Reject plan
- GET /api/v1/plans/{plan_id}/status - Execution status

All endpoints require authentication and RBAC.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.composition.goal_planner import GoalPlanner, TaskGraph, TaskNode
from src.agent.llm import LLMClient
from src.agent.registry import get_registry
from src.agent.tools import ToolGateway
from src.auth.dependencies import get_current_user
from src.config import Settings, get_settings
from src.core.policy import Permission, apply_tenant_filter, check_permission
from src.database import get_db_session
from src.models.plan import PlanRecord
from src.models.user import User

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class CreatePlanRequest(BaseModel):
    """Request to create an execution plan."""

    goal: str = Field(..., min_length=10, max_length=1000, description="High-level goal to decompose")
    context: str | None = Field(None, description="Additional context for planning")


class TaskNodeResponse(BaseModel):
    """Response model for a task node."""

    id: str
    description: str
    agent_id: str
    dependencies: list[str]
    status: str
    result_summary: str | None = None


class PlanResponse(BaseModel):
    """Response model for a plan."""

    plan_id: str
    goal: str
    status: str  # draft, approved, rejected, executing, complete, failed
    created_at: datetime
    tasks: list[TaskNodeResponse]
    execution_plan: str
    metadata: dict[str, Any]


class ApprovalRequest(BaseModel):
    """Request to approve or reject a plan."""

    comment: str | None = None


class ExecutionStatusResponse(BaseModel):
    """Response for execution status."""

    plan_id: str
    status: str
    progress: dict[str, Any]
    completed_tasks: int
    total_tasks: int


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _graph_to_json(graph: TaskGraph) -> dict[str, Any]:
    """Serialise a TaskGraph to a plain dict suitable for JSONB storage.

    Each TaskNode is stored with its public fields. The ``result`` field
    (AgentResponse) is not needed for plan persistence — only the
    ``result_summary`` (first 200 chars of content) is retained so that
    the status endpoint can report progress without loading heavyweight
    objects.
    """
    nodes: dict[str, Any] = {}
    for task_id, node in graph.nodes.items():
        result_content: str | None = None
        if node.result is not None:
            result_content = node.result.content[:200]
        nodes[task_id] = {
            "id": node.id,
            "description": node.description,
            "agent_id": node.agent_id,
            "dependencies": node.dependencies,
            "status": node.status,
            "result_content": result_content,
        }
    return {"nodes": nodes}


def _json_to_task_nodes(graph_json: dict[str, Any]) -> dict[str, TaskNode]:
    """Deserialise the stored graph JSON back into TaskNode objects.

    The ``result`` field is left as None — the raw result_content is
    surfaced directly in the response models.
    """
    nodes: dict[str, TaskNode] = {}
    for task_id, node_data in graph_json.get("nodes", {}).items():
        nodes[task_id] = TaskNode(
            id=node_data["id"],
            description=node_data["description"],
            agent_id=node_data["agent_id"],
            dependencies=node_data.get("dependencies", []),
            status=node_data.get("status", "pending"),
            result=None,
        )
    return nodes


def _build_tasks_response(
    graph_json: dict[str, Any],
    include_result: bool = False,
) -> list[TaskNodeResponse]:
    """Build a list of TaskNodeResponse objects from stored graph JSON."""
    tasks: list[TaskNodeResponse] = []
    for node_data in graph_json.get("nodes", {}).values():
        result_summary: str | None = None
        if include_result:
            result_summary = node_data.get("result_content")
        tasks.append(
            TaskNodeResponse(
                id=node_data["id"],
                description=node_data["description"],
                agent_id=node_data["agent_id"],
                dependencies=node_data.get("dependencies", []),
                status=node_data.get("status", "pending"),
                result_summary=result_summary,
            )
        )
    return tasks


def _plan_record_to_response(record: PlanRecord, include_result: bool = False) -> PlanResponse:
    """Convert a PlanRecord ORM object to a PlanResponse."""
    return PlanResponse(
        plan_id=str(record.id),
        goal=record.goal,
        status=record.status,
        created_at=record.created_at,
        tasks=_build_tasks_response(record.graph_json, include_result=include_result),
        execution_plan=record.execution_plan,
        metadata=record.metadata_json,
    )


async def _get_plan_or_404(
    plan_id: str,
    user: User,
    db: AsyncSession,
) -> PlanRecord:
    """Fetch a plan by ID with tenant isolation, or raise HTTP 404/403."""
    try:
        plan_uuid = uuid.UUID(plan_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {plan_id} not found",
        )

    stmt = apply_tenant_filter(
        select(PlanRecord).where(PlanRecord.id == plan_uuid),
        PlanRecord,
        user.tenant_id,
    )
    result = await db.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan {plan_id} not found",
        )

    return record


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=PlanResponse, status_code=status.HTTP_201_CREATED)
async def create_plan(
    request: CreatePlanRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> PlanResponse:
    """Create an execution plan from a high-level goal.

    This endpoint decomposes the goal into a task graph using the GoalPlanner.
    The plan is created in "draft" status and requires approval before execution.

    Requires: OPERATOR role or higher
    """
    # Check permission
    if not check_permission(user.role, Permission.CREATE_PLAN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions to create plans",
        )

    log.info(
        "plans.create_start",
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        goal_length=len(request.goal),
    )

    # Initialize dependencies
    llm_client = LLMClient(settings)
    tool_gateway = ToolGateway()
    registry = get_registry()

    # Create goal planner
    planner = GoalPlanner(
        db=db,
        settings=settings,
        llm_client=llm_client,
        tool_gateway=tool_gateway,
        registry=registry,
    )

    try:
        # Decompose goal into task graph
        graph = await planner.decompose(
            goal=request.goal,
            user_role=user.role,
        )

        # Validate graph
        if not planner.validate_graph(graph):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Generated task graph is invalid (contains cycles or missing dependencies)",
            )

        # Generate execution plan text
        execution_plan = planner.get_execution_plan(graph)

        # Persist plan to PostgreSQL
        now = datetime.now(UTC)
        record = PlanRecord(
            tenant_id=user.tenant_id,
            created_by=user.id,
            goal=request.goal,
            status="draft",
            graph_json=_graph_to_json(graph),
            execution_plan=execution_plan,
            metadata_json={"context": request.context},
            created_at=now,
            updated_at=now,
        )
        db.add(record)
        await db.flush()  # Populate record.id without committing yet

        log.info(
            "plans.create_complete",
            plan_id=str(record.id),
            task_count=len(graph.nodes),
        )

        return _plan_record_to_response(record)

    except HTTPException:
        raise
    except Exception as exc:
        log.error("plans.create_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create plan: {exc}",
        )


@router.get("/{plan_id}", response_model=PlanResponse)
async def get_plan(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PlanResponse:
    """Get plan details.

    Requires: User must own the plan (same tenant)
    """
    record = await _get_plan_or_404(plan_id, user, db)
    return _plan_record_to_response(record, include_result=True)


@router.post("/{plan_id}/approve", response_model=PlanResponse)
async def approve_plan(
    plan_id: str,
    request: ApprovalRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> PlanResponse:
    """Approve a plan for execution.

    Transitions plan from "draft" to "approved" status.
    Execution can then be triggered.

    Requires: ENGINEER role or higher
    """
    # Check permission
    if not check_permission(user.role, Permission.APPROVE_PLAN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions to approve plans",
        )

    record = await _get_plan_or_404(plan_id, user, db)

    # Check current status
    if record.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve plan in status '{record.status}'",
        )

    log.info(
        "plans.approve",
        plan_id=plan_id,
        user_id=str(user.id),
        comment=request.comment,
    )

    # Update status fields
    now = datetime.now(UTC)
    record.status = "approved"
    record.approved_at = now
    record.approved_by = user.id
    record.updated_at = now

    if request.comment:
        # metadata_json is JSONB — reassign to trigger SQLAlchemy change tracking
        updated_metadata = dict(record.metadata_json)
        updated_metadata["approval_comment"] = request.comment
        record.metadata_json = updated_metadata

    await db.flush()

    return _plan_record_to_response(record)


@router.post("/{plan_id}/reject", response_model=PlanResponse)
async def reject_plan(
    plan_id: str,
    request: ApprovalRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PlanResponse:
    """Reject a plan.

    Transitions plan from "draft" to "rejected" status.

    Requires: ENGINEER role or higher
    """
    # Check permission
    if not check_permission(user.role, Permission.APPROVE_PLAN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions to reject plans",
        )

    record = await _get_plan_or_404(plan_id, user, db)

    # Check current status
    if record.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot reject plan in status '{record.status}'",
        )

    log.info(
        "plans.reject",
        plan_id=plan_id,
        user_id=str(user.id),
        comment=request.comment,
    )

    # Update status fields
    now = datetime.now(UTC)
    record.status = "rejected"
    record.rejected_at = now
    record.rejected_by = user.id
    record.updated_at = now

    if request.comment:
        # metadata_json is JSONB — reassign to trigger SQLAlchemy change tracking
        updated_metadata = dict(record.metadata_json)
        updated_metadata["rejection_comment"] = request.comment
        record.metadata_json = updated_metadata

    await db.flush()

    return _plan_record_to_response(record)


@router.get("/{plan_id}/status", response_model=ExecutionStatusResponse)
async def get_execution_status(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ExecutionStatusResponse:
    """Get execution status for a plan.

    Returns current execution progress, completed tasks, and status.

    Requires: User must own the plan (same tenant)
    """
    record = await _get_plan_or_404(plan_id, user, db)

    nodes = record.graph_json.get("nodes", {})

    # Count completed tasks
    completed_tasks = sum(
        1 for node_data in nodes.values() if node_data.get("status") == "complete"
    )
    total_tasks = len(nodes)

    # Build per-task progress snapshot
    progress_info: dict[str, Any] = {
        "tasks": {
            node_id: {
                "status": node_data.get("status", "pending"),
                "description": node_data.get("description", ""),
                "agent_id": node_data.get("agent_id", ""),
            }
            for node_id, node_data in nodes.items()
        }
    }

    return ExecutionStatusResponse(
        plan_id=plan_id,
        status=record.status,
        progress=progress_info,
        completed_tasks=completed_tasks,
        total_tasks=total_tasks,
    )
