"""Tests for plans API endpoint."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.plans import router
from src.auth.dependencies import get_current_user
from src.database import get_db_session
from src.models.user import User, UserRole

# Import all models to ensure SQLAlchemy relationships are resolved
import src.models.tenant  # noqa: F401
import src.models.idp_config  # noqa: F401
import src.models.plan  # noqa: F401


def make_mock_plan_record(
    plan_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    goal: str = "Test goal",
    status: str = "draft",
):
    """Create a mock PlanRecord ORM object."""
    record = MagicMock()
    record.id = plan_id
    record.tenant_id = tenant_id
    record.created_by = user_id
    record.goal = goal
    record.status = status
    record.created_at = datetime.now(timezone.utc)
    record.graph_json = {}
    record.execution_plan = "Test plan"
    record.metadata_json = {}
    return record


def make_mock_db_that_returns(plan_record=None):
    """Create mock DB session that returns given plan_record from execute()."""
    mock_db = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = plan_record
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()
    mock_db.rollback = AsyncMock()
    mock_db.flush = AsyncMock()
    mock_db.close = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.delete = MagicMock()
    return mock_db


@pytest.fixture
def app(test_user, mock_db_session, fake_settings):
    """Create FastAPI test application with operator user."""
    from src.config import get_settings

    test_app = FastAPI()
    test_app.include_router(router)

    test_app.dependency_overrides[get_current_user] = lambda: test_user
    test_app.dependency_overrides[get_db_session] = lambda: mock_db_session
    test_app.dependency_overrides[get_settings] = lambda: fake_settings

    return test_app


@pytest.fixture
def admin_app(test_admin_user, fake_settings):
    """Create FastAPI test application with admin user and configurable DB."""
    from src.config import get_settings

    test_app = FastAPI()
    test_app.include_router(router)

    test_app.dependency_overrides[get_current_user] = lambda: test_admin_user
    test_app.dependency_overrides[get_settings] = lambda: fake_settings

    return test_app


class TestPlanEndpoints:
    """Test plan creation and approval endpoints."""

    @pytest.mark.asyncio
    async def test_create_plan_succeeds_for_operator(
        self, app, test_user, fake_settings
    ):
        """Test that operators can create execution plans."""
        test_user.role = UserRole.OPERATOR

        # Create a mock plan record to return from DB add/flush
        plan_id = uuid.uuid4()
        mock_db = make_mock_db_that_returns(None)

        from src.config import get_settings

        new_app = FastAPI()
        new_app.include_router(router)
        new_app.dependency_overrides[get_current_user] = lambda: test_user
        new_app.dependency_overrides[get_db_session] = lambda: mock_db
        new_app.dependency_overrides[get_settings] = lambda: fake_settings

        async with AsyncClient(transport=httpx.ASGITransport(app=new_app), base_url="http://test") as ac:
            with patch("src.api.plans.GoalPlanner") as mock_planner_cls:
                mock_planner = MagicMock()
                mock_graph = MagicMock()
                mock_graph.nodes = {}
                mock_planner.decompose = AsyncMock(return_value=mock_graph)
                mock_planner.validate_graph.return_value = True
                mock_planner.get_execution_plan.return_value = "Test plan"
                mock_planner_cls.return_value = mock_planner

                # Mock the PlanRecord creation
                with patch("src.api.plans.PlanRecord") as mock_plan_record_cls:
                    mock_record = MagicMock()
                    mock_record.id = plan_id
                    mock_record.goal = "Complete the quarterly report"
                    mock_record.status = "draft"
                    mock_record.created_at = datetime.now(timezone.utc)
                    mock_record.graph_json = {}
                    mock_record.execution_plan = "Test plan"
                    mock_record.metadata_json = {}
                    mock_plan_record_cls.return_value = mock_record

                    response = await ac.post(
                        "/api/v1/plans",
                        json={
                            "goal": "Complete the quarterly report",
                            "context": "Q4 2025 financial data",
                        },
                    )

        assert response.status_code == 201
        data = response.json()
        assert "plan_id" in data
        assert data["status"] == "draft"
        assert data["goal"] == "Complete the quarterly report"

    @pytest.mark.asyncio
    async def test_create_plan_rejects_viewer_role(
        self, test_viewer_user, mock_db_session, fake_settings
    ):
        """Test that viewers cannot create plans."""
        from src.config import get_settings

        viewer_app = FastAPI()
        viewer_app.include_router(router)
        viewer_app.dependency_overrides[get_current_user] = lambda: test_viewer_user
        viewer_app.dependency_overrides[get_db_session] = lambda: mock_db_session
        viewer_app.dependency_overrides[get_settings] = lambda: fake_settings

        async with AsyncClient(transport=httpx.ASGITransport(app=viewer_app), base_url="http://test") as ac:
            response = await ac.post(
                "/api/v1/plans",
                json={"goal": "Test goal for viewer rejection"},
            )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_approve_plan_transitions_to_approved(
        self, admin_app, test_admin_user, test_tenant_id
    ):
        """Test that approving a plan transitions it to approved status."""
        plan_id = uuid.uuid4()
        mock_record = make_mock_plan_record(
            plan_id=plan_id,
            tenant_id=test_tenant_id,
            user_id=test_admin_user.id,
        )
        mock_db = make_mock_db_that_returns(mock_record)
        admin_app.dependency_overrides[get_db_session] = lambda: mock_db

        async with AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="http://test") as ac:
            response = await ac.post(
                f"/api/v1/plans/{plan_id}/approve",
                json={"comment": "Looks good"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "approved"

    @pytest.mark.asyncio
    async def test_reject_plan_transitions_to_rejected(
        self, admin_app, test_admin_user, test_tenant_id
    ):
        """Test that rejecting a plan transitions it to rejected status."""
        plan_id = uuid.uuid4()
        mock_record = make_mock_plan_record(
            plan_id=plan_id,
            tenant_id=test_tenant_id,
            user_id=test_admin_user.id,
        )
        mock_db = make_mock_db_that_returns(mock_record)
        admin_app.dependency_overrides[get_db_session] = lambda: mock_db

        async with AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="http://test") as ac:
            response = await ac.post(
                f"/api/v1/plans/{plan_id}/reject",
                json={"comment": "Needs revision"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_get_plan_returns_404_for_nonexistent_plan(
        self, test_user, fake_settings
    ):
        """Test that getting non-existent plan returns 404."""
        from src.config import get_settings

        mock_db = make_mock_db_that_returns(None)  # No plan found

        no_plan_app = FastAPI()
        no_plan_app.include_router(router)
        no_plan_app.dependency_overrides[get_current_user] = lambda: test_user
        no_plan_app.dependency_overrides[get_db_session] = lambda: mock_db
        no_plan_app.dependency_overrides[get_settings] = lambda: fake_settings

        nonexistent_id = str(uuid.uuid4())
        async with AsyncClient(transport=httpx.ASGITransport(app=no_plan_app), base_url="http://test") as ac:
            response = await ac.get(f"/api/v1/plans/{nonexistent_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_plan_returns_plan_for_owner(
        self, test_user, test_tenant_id, fake_settings
    ):
        """Test getting a plan that belongs to the user's tenant."""
        from src.config import get_settings

        plan_id = uuid.uuid4()
        mock_record = make_mock_plan_record(
            plan_id=plan_id,
            tenant_id=test_tenant_id,
            user_id=test_user.id,
        )
        mock_db = make_mock_db_that_returns(mock_record)

        owner_app = FastAPI()
        owner_app.include_router(router)
        owner_app.dependency_overrides[get_current_user] = lambda: test_user
        owner_app.dependency_overrides[get_db_session] = lambda: mock_db
        owner_app.dependency_overrides[get_settings] = lambda: fake_settings

        async with AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://test") as ac:
            response = await ac.get(f"/api/v1/plans/{plan_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["plan_id"] == str(plan_id)
