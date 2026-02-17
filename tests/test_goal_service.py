"""Tests for GoalService (Phase 11E1).

Covers CRUD operations for persistent user goals with full tenant isolation.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.goal_service import GoalNotFoundError, GoalService


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


class TestCreateGoal:
    async def test_create_goal_returns_goal(self, mock_db, tenant_id, user_id):
        service = GoalService(mock_db)
        goal = await service.create_goal(
            tenant_id=tenant_id,
            user_id=user_id,
            goal_text="Learn about RAG pipelines",
        )
        assert goal.tenant_id == tenant_id
        assert goal.user_id == user_id
        assert goal.goal_text == "Learn about RAG pipelines"
        assert goal.status == "active"

    async def test_create_goal_adds_to_db(self, mock_db, tenant_id, user_id):
        service = GoalService(mock_db)
        await service.create_goal(
            tenant_id=tenant_id,
            user_id=user_id,
            goal_text="Deploy the new service",
        )
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    async def test_create_goal_default_status_is_active(self, mock_db, tenant_id, user_id):
        service = GoalService(mock_db)
        goal = await service.create_goal(
            tenant_id=tenant_id,
            user_id=user_id,
            goal_text="Any goal",
        )
        assert goal.status == "active"

    async def test_create_goal_generates_uuid(self, mock_db, tenant_id, user_id):
        service = GoalService(mock_db)
        goal = await service.create_goal(
            tenant_id=tenant_id,
            user_id=user_id,
            goal_text="Goal with auto-id",
        )
        assert isinstance(goal.id, uuid.UUID)


class TestGetActiveGoals:
    async def test_get_active_goals_returns_list(self, mock_db, tenant_id, user_id):
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = GoalService(mock_db)
        goals = await service.get_active_goals(tenant_id=tenant_id, user_id=user_id)
        assert isinstance(goals, list)

    async def test_get_active_goals_queries_db(self, mock_db, tenant_id, user_id):
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = GoalService(mock_db)
        await service.get_active_goals(tenant_id=tenant_id, user_id=user_id)
        mock_db.execute.assert_called_once()


class TestUpdateGoalProgress:
    async def _make_mock_goal(self, goal_id: uuid.UUID, tenant_id: uuid.UUID) -> MagicMock:
        goal = MagicMock()
        goal.id = goal_id
        goal.tenant_id = tenant_id
        goal.progress_notes = None
        return goal

    async def test_update_progress_sets_notes_when_empty(self, mock_db, tenant_id, user_id):
        goal_id = uuid.uuid4()
        goal = await self._make_mock_goal(goal_id, tenant_id)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = goal
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = GoalService(mock_db)
        updated = await service.update_goal_progress(
            goal_id=goal_id,
            notes="Made initial progress",
            tenant_id=tenant_id,
            user_id=user_id,
        )
        assert updated.progress_notes == "Made initial progress"

    async def test_update_progress_appends_when_existing(self, mock_db, tenant_id, user_id):
        goal_id = uuid.uuid4()
        goal = await self._make_mock_goal(goal_id, tenant_id)
        goal.progress_notes = "First note"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = goal
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = GoalService(mock_db)
        updated = await service.update_goal_progress(
            goal_id=goal_id,
            notes="Second note",
            tenant_id=tenant_id,
            user_id=user_id,
        )
        assert "First note" in updated.progress_notes
        assert "Second note" in updated.progress_notes

    async def test_update_progress_raises_when_not_found(self, mock_db, tenant_id, user_id):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = GoalService(mock_db)
        with pytest.raises(GoalNotFoundError):
            await service.update_goal_progress(
                goal_id=uuid.uuid4(),
                notes="notes",
                tenant_id=tenant_id,
                user_id=user_id,
            )


class TestCompleteGoal:
    async def test_complete_goal_sets_status(self, mock_db, tenant_id, user_id):
        goal_id = uuid.uuid4()
        goal = MagicMock()
        goal.id = goal_id
        goal.tenant_id = tenant_id
        goal.status = "active"
        goal.completed_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = goal
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = GoalService(mock_db)
        updated = await service.complete_goal(
            goal_id=goal_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        assert updated.status == "completed"
        assert updated.completed_at is not None

    async def test_complete_goal_raises_when_not_found(self, mock_db, tenant_id, user_id):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = GoalService(mock_db)
        with pytest.raises(GoalNotFoundError):
            await service.complete_goal(
                goal_id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
            )


class TestGoalModel:
    def test_user_goal_init_defaults(self):
        from src.models.user_goal import UserGoal
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        goal = UserGoal(
            tenant_id=tenant_id,
            user_id=user_id,
            goal_text="Test goal",
        )
        assert goal.status == "active"
        assert goal.progress_notes is None
        assert goal.completed_at is None
        assert isinstance(goal.id, uuid.UUID)

    def test_user_goal_repr(self):
        from src.models.user_goal import UserGoal
        goal = UserGoal(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            goal_text="My goal",
        )
        repr_str = repr(goal)
        assert "UserGoal" in repr_str
        assert "active" in repr_str
