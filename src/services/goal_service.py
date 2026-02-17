"""Goal service — CRUD operations for persistent user goals.

All operations are tenant-scoped.  The agent uses this service to:
- Inject active goals into the system prompt at conversation start
- Record incremental progress notes after each response
- List/create/complete goals via the REST API

Users complete goals manually.  The agent only appends progress_notes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.policy import apply_tenant_filter
from src.models.user_goal import UserGoal

log = structlog.get_logger(__name__)


class GoalNotFoundError(Exception):
    """Raised when a requested goal does not exist or is not accessible."""


class GoalService:
    """Service for managing persistent user goals."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialise goal service with database session.

        Args:
            db: Async database session
        """
        self._db = db

    async def get_active_goals(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[UserGoal]:
        """Return all active goals for a user within a tenant.

        Args:
            tenant_id: Tenant UUID for isolation
            user_id: User UUID to filter goals

        Returns:
            List of active UserGoal records ordered by creation time
        """
        stmt = apply_tenant_filter(
            select(UserGoal)
            .where(
                UserGoal.user_id == user_id,
                UserGoal.status == "active",
            )
            .order_by(UserGoal.created_at.asc()),
            UserGoal,
            tenant_id,
        )
        result = await self._db.execute(stmt)
        goals = list(result.scalars().all())

        log.debug(
            "goal_service.get_active_goals",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            count=len(goals),
        )

        return goals

    async def create_goal(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        goal_text: str,
    ) -> UserGoal:
        """Create a new active goal for a user.

        Args:
            tenant_id: Tenant UUID for isolation
            user_id: User UUID who owns the goal
            goal_text: Free-text description of the goal

        Returns:
            Newly created UserGoal record
        """
        goal = UserGoal(
            tenant_id=tenant_id,
            user_id=user_id,
            goal_text=goal_text,
            status="active",
        )
        self._db.add(goal)
        await self._db.flush()

        log.info(
            "goal_service.create_goal",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            goal_id=str(goal.id),
        )

        return goal

    async def update_goal_progress(
        self,
        goal_id: uuid.UUID,
        notes: str,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> UserGoal:
        """Append progress notes to an existing goal.

        Concatenates the new notes with any existing notes so the full
        progress history is preserved.

        Args:
            goal_id: UUID of the goal to update
            notes: Progress notes to append
            tenant_id: Tenant UUID for isolation check
            user_id: User UUID — only the goal owner may update progress

        Returns:
            Updated UserGoal record

        Raises:
            GoalNotFoundError: If the goal does not exist or belongs to a different user
        """
        goal = await self._get_goal(goal_id, tenant_id=tenant_id, user_id=user_id)

        # Append to existing notes rather than replace
        if goal.progress_notes:
            goal.progress_notes = f"{goal.progress_notes}\n{notes}"
        else:
            goal.progress_notes = notes

        goal.updated_at = datetime.now(UTC)
        await self._db.flush()

        log.info(
            "goal_service.update_goal_progress",
            goal_id=str(goal_id),
        )

        return goal

    async def complete_goal(
        self,
        goal_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> UserGoal:
        """Mark a goal as completed.

        Args:
            goal_id: UUID of the goal to complete
            tenant_id: Tenant UUID for isolation check
            user_id: User UUID — only the goal owner may complete it

        Returns:
            Updated UserGoal record

        Raises:
            GoalNotFoundError: If the goal does not exist or belongs to a different user
        """
        goal = await self._get_goal(goal_id, tenant_id=tenant_id, user_id=user_id)

        goal.status = "completed"
        goal.completed_at = datetime.now(UTC)
        goal.updated_at = datetime.now(UTC)
        await self._db.flush()

        log.info(
            "goal_service.complete_goal",
            goal_id=str(goal_id),
        )

        return goal

    async def abandon_goal(
        self,
        goal_id: uuid.UUID,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> UserGoal:
        """Mark a goal as abandoned.

        Args:
            goal_id: UUID of the goal to abandon
            tenant_id: Tenant UUID for isolation check
            user_id: User UUID -- only the goal owner may abandon it

        Returns:
            Updated UserGoal record

        Raises:
            GoalNotFoundError: If the goal does not exist or belongs to a different user
        """
        goal = await self._get_goal(goal_id, tenant_id=tenant_id, user_id=user_id)

        goal.status = "abandoned"
        goal.updated_at = datetime.now(UTC)
        await self._db.flush()

        log.info(
            "goal_service.abandon_goal",
            goal_id=str(goal_id),
        )

        return goal

    async def _get_goal(
        self,
        goal_id: uuid.UUID,
        tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> UserGoal:
        """Fetch a goal by ID, scoped to a tenant and user.

        Args:
            goal_id: UUID of the goal
            tenant_id: Optional tenant for isolation check
            user_id: Optional user for ownership check

        Returns:
            UserGoal record

        Raises:
            GoalNotFoundError: If not found or not owned by the given user
        """
        stmt = select(UserGoal).where(UserGoal.id == goal_id)

        if tenant_id is not None:
            stmt = apply_tenant_filter(stmt, UserGoal, tenant_id)

        if user_id is not None:
            stmt = stmt.where(UserGoal.user_id == user_id)

        result = await self._db.execute(stmt)
        goal = result.scalar_one_or_none()

        if goal is None:
            raise GoalNotFoundError(f"Goal {goal_id} not found")

        return goal
