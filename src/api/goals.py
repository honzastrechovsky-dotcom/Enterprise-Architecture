"""User Goals API endpoints.

GET    /api/v1/goals                   - List user's active goals
POST   /api/v1/goals                   - Create a new goal
PATCH  /api/v1/goals/{id}/progress     - Update progress notes
POST   /api/v1/goals/{id}/complete     - Mark goal as completed

All endpoints are tenant-scoped; only the authenticated user's goals are
returned.  Operators and above can write; viewers can only read.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.api_key_auth import require_scope
from src.auth.dependencies import get_current_user
from src.core.policy import Permission, check_permission
from src.database import get_db_session
from src.models.user import User
from src.services.goal_service import GoalNotFoundError, GoalService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/goals", tags=["goals"], dependencies=[Depends(require_scope("goals"))])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateGoalRequest(BaseModel):
    goal_text: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Free-text description of the goal",
    )


class UpdateProgressRequest(BaseModel):
    notes: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Progress notes to append to the goal",
    )


class GoalResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    goal_text: str
    status: str
    progress_notes: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[GoalResponse])
async def list_active_goals(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[GoalResponse]:
    """List all active goals for the authenticated user.

    Returns goals in creation order (oldest first).
    """
    service = GoalService(db)
    goals = await service.get_active_goals(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )

    log.info(
        "goals.list",
        user_id=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
        count=len(goals),
    )

    return [GoalResponse.model_validate(g) for g in goals]


@router.post("", response_model=GoalResponse, status_code=status.HTTP_201_CREATED)
async def create_goal(
    request: CreateGoalRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> GoalResponse:
    """Create a new goal for the authenticated user.

    Goals start with status 'active' and are tracked across conversations.
    """
    check_permission(current_user.role, Permission.WRITE)

    service = GoalService(db)
    goal = await service.create_goal(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        goal_text=request.goal_text,
    )

    log.info(
        "goals.created",
        user_id=str(current_user.id),
        goal_id=str(goal.id),
    )

    return GoalResponse.model_validate(goal)


@router.patch("/{goal_id}/progress", response_model=GoalResponse)
async def update_goal_progress(
    goal_id: uuid.UUID,
    request: UpdateProgressRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> GoalResponse:
    """Append progress notes to a goal.

    Notes are appended (not replaced) to preserve full history.
    """
    check_permission(current_user.role, Permission.WRITE)

    service = GoalService(db)
    try:
        goal = await service.update_goal_progress(
            goal_id=goal_id,
            notes=request.notes,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
        )
    except GoalNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Goal {goal_id} not found",
        )

    log.info(
        "goals.progress_updated",
        user_id=str(current_user.id),
        goal_id=str(goal_id),
    )

    return GoalResponse.model_validate(goal)


@router.post("/{goal_id}/complete", response_model=GoalResponse)
async def complete_goal(
    goal_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> GoalResponse:
    """Mark a goal as completed.

    Only the goal owner (or an admin) should complete goals.
    The agent never auto-completes goals.
    """
    check_permission(current_user.role, Permission.WRITE)

    service = GoalService(db)
    try:
        goal = await service.complete_goal(
            goal_id=goal_id,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
        )
    except GoalNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Goal {goal_id} not found",
        )

    log.info(
        "goals.completed",
        user_id=str(current_user.id),
        goal_id=str(goal_id),
    )

    return GoalResponse.model_validate(goal)
