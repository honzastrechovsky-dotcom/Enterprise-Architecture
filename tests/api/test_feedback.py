"""Tests for feedback API endpoints.

Following TDD methodology - these tests define expected behavior BEFORE implementation.
They should FAIL until implementation is complete (RED phase).
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.database import get_db_session
from src.models.user import UserRole

# Import all models to ensure SQLAlchemy relationships are resolved
import src.models.tenant  # noqa: F401
import src.models.idp_config  # noqa: F401


@pytest.fixture
def mock_auth_user(test_user):
    """Create an AuthenticatedUser wrapping test_user (operator role)."""
    return AuthenticatedUser(user=test_user, claims={"sub": test_user.external_id})


@pytest.fixture
def mock_admin_auth_user(test_admin_user):
    """Create an AuthenticatedUser wrapping test_admin_user (admin role)."""
    return AuthenticatedUser(user=test_admin_user, claims={"sub": "admin-ext"})


@pytest.fixture
def app(mock_auth_user, mock_db_session, fake_settings):
    """Create FastAPI test application with operator user (dependency overrides)."""
    from src.api.feedback import router
    from src.config import get_settings

    test_app = FastAPI()
    test_app.include_router(router)

    # Override core dependencies
    test_app.dependency_overrides[get_current_user] = lambda: mock_auth_user
    test_app.dependency_overrides[get_db_session] = lambda: mock_db_session
    test_app.dependency_overrides[get_settings] = lambda: fake_settings

    return test_app


@pytest.fixture
def admin_app(mock_admin_auth_user, mock_db_session, fake_settings):
    """Create FastAPI test application with admin user (dependency overrides)."""
    from src.api.feedback import router
    from src.config import get_settings

    test_app = FastAPI()
    test_app.include_router(router)

    # Override core dependencies
    test_app.dependency_overrides[get_current_user] = lambda: mock_admin_auth_user
    test_app.dependency_overrides[get_db_session] = lambda: mock_db_session
    test_app.dependency_overrides[get_settings] = lambda: fake_settings

    return test_app


class TestSubmitFeedback:
    """Test POST /api/v1/feedback - Submit feedback."""

    @pytest.mark.asyncio
    async def test_submit_thumbs_up_feedback(
        self, app, fake_settings
    ):
        """Test submitting positive feedback with thumbs up."""
        feedback_id = uuid.uuid4()

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.feedback.FeedbackService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.submit_feedback = AsyncMock(return_value=feedback_id)
                mock_service_cls.return_value = mock_service

                response = await ac.post(
                    "/api/v1/feedback",
                    json={
                        "rating": "thumbs_up",
                        "prompt_text": "What is the capital of France?",
                        "response_text": "The capital of France is Paris.",
                        "model_used": "openai/gpt-4o",
                        "comment": "Very accurate response!",
                        "tags": ["accurate", "helpful"],
                    },
                )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == str(feedback_id)
        assert data["rating"] == "thumbs_up"

    @pytest.mark.asyncio
    async def test_submit_thumbs_down_feedback_with_trace_id(
        self, app, fake_settings
    ):
        """Test submitting negative feedback with optional trace_id."""
        feedback_id = uuid.uuid4()
        trace_id = "trace-abc-123"

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.feedback.FeedbackService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.submit_feedback = AsyncMock(return_value=feedback_id)
                mock_service_cls.return_value = mock_service

                response = await ac.post(
                    "/api/v1/feedback",
                    json={
                        "rating": "thumbs_down",
                        "prompt_text": "Calculate 2+2",
                        "response_text": "5",
                        "model_used": "openai/gpt-4o-mini",
                        "trace_id": trace_id,
                        "tags": ["incorrect", "math_error"],
                    },
                )

        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_submit_feedback_requires_authentication(self, mock_db_session, fake_settings):
        """Test that unauthenticated requests are rejected."""
        from src.api.feedback import router
        from src.config import get_settings

        # App without auth override
        unauth_app = FastAPI()
        unauth_app.include_router(router)
        unauth_app.dependency_overrides[get_db_session] = lambda: mock_db_session
        unauth_app.dependency_overrides[get_settings] = lambda: fake_settings

        async with AsyncClient(transport=httpx.ASGITransport(app=unauth_app), base_url="http://test") as ac:
            response = await ac.post(
                "/api/v1/feedback",
                json={
                    "rating": "thumbs_up",
                    "prompt_text": "test",
                    "response_text": "test",
                    "model_used": "test-model",
                },
            )
            assert response.status_code in [401, 422]

    @pytest.mark.asyncio
    async def test_submit_feedback_with_rating_scale(
        self, app, fake_settings
    ):
        """Test submitting feedback with 1-5 rating scale."""
        feedback_id = uuid.uuid4()

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.feedback.FeedbackService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.submit_feedback = AsyncMock(return_value=feedback_id)
                mock_service_cls.return_value = mock_service

                response = await ac.post(
                    "/api/v1/feedback",
                    json={
                        "rating": "rating_4",
                        "prompt_text": "Explain quantum physics",
                        "response_text": "Quantum physics is...",
                        "model_used": "openai/gpt-4o",
                    },
                )

        assert response.status_code == 201


class TestListFeedback:
    """Test GET /api/v1/feedback - List feedback."""

    @pytest.mark.asyncio
    async def test_list_feedback_returns_tenant_scoped_results(
        self, app, test_user, fake_settings
    ):
        """Test that feedback list is scoped to user's tenant."""
        mock_feedback = [
            {
                "id": uuid.uuid4(),
                "tenant_id": test_user.tenant_id,
                "user_id": test_user.id,
                "rating": "thumbs_up",
                "prompt_text": "test prompt",
                "response_text": "test response",
                "model_used": "gpt-4o",
                "created_at": datetime.now(timezone.utc),
                "conversation_id": None,
                "message_id": None,
                "trace_id": None,
                "comment": None,
                "tags": [],
            }
        ]

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.feedback.FeedbackService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.list_feedback = AsyncMock(return_value=mock_feedback)
                mock_service_cls.return_value = mock_service

                response = await ac.get("/api/v1/feedback")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_list_feedback_with_filters(
        self, app, fake_settings
    ):
        """Test filtering feedback by rating and date range."""
        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.feedback.FeedbackService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.list_feedback = AsyncMock(return_value=[])
                mock_service_cls.return_value = mock_service

                response = await ac.get(
                    "/api/v1/feedback",
                    params={
                        "rating": "thumbs_up",
                        "limit": 10,
                        "offset": 0,
                    },
                )

        assert response.status_code == 200


class TestFeedbackStats:
    """Test GET /api/v1/feedback/stats - Feedback statistics."""

    @pytest.mark.asyncio
    async def test_get_feedback_stats(
        self, app, fake_settings
    ):
        """Test retrieving feedback statistics."""
        mock_stats = {
            "total_count": 100,
            "positive_rate": 0.85,
            "top_tags": [
                {"tag": "accurate", "count": 45},
                {"tag": "helpful", "count": 38},
            ],
            "by_model": {
                "openai/gpt-4o": {"count": 60, "positive_rate": 0.90},
                "openai/gpt-4o-mini": {"count": 40, "positive_rate": 0.78},
            },
        }

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.feedback.FeedbackService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.get_feedback_stats = AsyncMock(return_value=mock_stats)
                mock_service_cls.return_value = mock_service

                response = await ac.get("/api/v1/feedback/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 100
        assert data["positive_rate"] == 0.85


class TestExportFeedback:
    """Test GET /api/v1/feedback/export - Export feedback."""

    @pytest.mark.asyncio
    async def test_export_feedback_as_jsonl(
        self, admin_app, fake_settings
    ):
        """Test exporting feedback in JSONL format (requires admin)."""
        mock_jsonl = '{"prompt": "test", "response": "test response"}\n'

        async with AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="http://test") as ac:
            with patch("src.api.feedback.FeedbackService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.export_feedback = AsyncMock(return_value=mock_jsonl)
                mock_service_cls.return_value = mock_service

                response = await ac.get(
                    "/api/v1/feedback/export",
                    params={"format": "jsonl", "rating": "thumbs_up"},
                )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-ndjson"


class TestFinetuningDatasets:
    """Test fine-tuning dataset management endpoints."""

    @pytest.mark.asyncio
    async def test_create_finetuning_dataset(
        self, app, fake_settings
    ):
        """Test creating a fine-tuning dataset from feedback (requires operator)."""
        dataset_id = uuid.uuid4()

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.feedback.FinetuningService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.create_dataset = AsyncMock(return_value=dataset_id)
                mock_service.populate_dataset = AsyncMock(return_value=None)
                mock_service_cls.return_value = mock_service

                response = await ac.post(
                    "/api/v1/finetuning/datasets",
                    json={
                        "name": "High-quality responses Q1 2026",
                        "description": "Positive feedback from Q1",
                        "filters": {
                            "min_rating": "thumbs_up",
                            "tags": ["accurate", "helpful"],
                        },
                    },
                )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == str(dataset_id)

    @pytest.mark.asyncio
    async def test_list_finetuning_datasets(
        self, app, fake_settings
    ):
        """Test listing fine-tuning datasets (requires operator)."""
        mock_datasets = [
            {
                "id": uuid.uuid4(),
                "name": "Dataset 1",
                "description": "Test dataset",
                "status": "ready",
                "record_count": 150,
                "filters": {},
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        ]

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.feedback.FinetuningService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.list_datasets = AsyncMock(return_value=mock_datasets)
                mock_service_cls.return_value = mock_service

                response = await ac.get("/api/v1/finetuning/datasets")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_get_finetuning_dataset(
        self, app, fake_settings
    ):
        """Test getting dataset details with sample records (requires operator)."""
        dataset_id = uuid.uuid4()
        mock_dataset = {
            "id": dataset_id,
            "name": "Test Dataset",
            "description": "Test dataset",
            "status": "ready",
            "record_count": 100,
            "filters": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "sample_records": [
                {
                    "system_prompt": "You are a helpful assistant",
                    "user_prompt": "What is 2+2?",
                    "assistant_response": "4",
                    "quality_score": 0.95,
                }
            ],
        }

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.feedback.FinetuningService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.get_dataset = AsyncMock(return_value=mock_dataset)
                mock_service_cls.return_value = mock_service

                response = await ac.get(f"/api/v1/finetuning/datasets/{dataset_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(dataset_id)
        assert "sample_records" in data

    @pytest.mark.asyncio
    async def test_export_finetuning_dataset(
        self, admin_app, fake_settings
    ):
        """Test exporting dataset in OpenAI fine-tuning format (requires admin)."""
        dataset_id = uuid.uuid4()
        mock_export = '{"messages": [{"role": "system", "content": "..."}, ...]}\n'

        async with AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="http://test") as ac:
            with patch("src.api.feedback.FinetuningService") as mock_service_cls:
                mock_service = MagicMock()
                mock_service.export_dataset = AsyncMock(return_value=mock_export)
                mock_service_cls.return_value = mock_service

                response = await ac.post(
                    f"/api/v1/finetuning/datasets/{dataset_id}/export",
                    json={"format": "openai"},
                )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-ndjson"
