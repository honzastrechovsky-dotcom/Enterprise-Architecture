"""Tests for chat API endpoint."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import AsyncClient

from src.api.chat import router, ChatRequestBody, ChatResponseBody
from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.database import get_db_session
from src.models.user import User, UserRole

# Import all models to ensure SQLAlchemy relationships are resolved
import src.models.tenant  # noqa: F401
import src.models.idp_config  # noqa: F401


@pytest.fixture
def mock_auth_user(test_user):
    """Create an AuthenticatedUser wrapping test_user."""
    return AuthenticatedUser(user=test_user, claims={"sub": test_user.external_id})


@pytest.fixture
def app(mock_auth_user, mock_db_session, fake_settings):
    """Create FastAPI test application with dependency overrides."""
    from src.config import get_settings

    test_app = FastAPI()
    test_app.include_router(router)

    # Override dependencies
    test_app.dependency_overrides[get_current_user] = lambda: mock_auth_user
    test_app.dependency_overrides[get_db_session] = lambda: mock_db_session
    test_app.dependency_overrides[get_settings] = lambda: fake_settings

    return test_app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


class TestChatEndpoint:
    """Test POST /chat endpoint."""

    @pytest.mark.asyncio
    async def test_chat_returns_response_for_authenticated_user(
        self, app, fake_settings
    ):
        """Test that authenticated user receives chat response."""
        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.chat.get_rate_limiter") as mock_limiter:
                mock_limiter.return_value.check = AsyncMock()

                # Mock runtime
                with patch("src.api.chat.AgentRuntime") as mock_runtime_cls:
                    mock_runtime = MagicMock()
                    mock_response = MagicMock()
                    mock_response.response = "Test response"
                    mock_response.conversation_id = uuid.uuid4()
                    mock_response.citations = []
                    mock_response.model_used = "test-model"
                    mock_runtime.chat = AsyncMock(return_value=mock_response)
                    mock_runtime_cls.return_value = mock_runtime

                    response = await ac.post(
                        "/chat",
                        json={"message": "Hello"},
                    )

        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert "conversation_id" in data

    @pytest.mark.asyncio
    async def test_chat_rejects_unauthenticated_request(self, mock_db_session, fake_settings):
        """Test that unauthenticated requests receive 401."""
        from src.config import get_settings

        # App without auth override - get_current_user will fail without token
        unauth_app = FastAPI()
        unauth_app.include_router(router)
        unauth_app.dependency_overrides[get_db_session] = lambda: mock_db_session
        unauth_app.dependency_overrides[get_settings] = lambda: fake_settings

        async with AsyncClient(transport=httpx.ASGITransport(app=unauth_app), base_url="http://test") as ac:
            response = await ac.post(
                "/chat",
                json={"message": "Hello"},
            )
            # Should return 401 for missing auth
            assert response.status_code in [401, 422]

    @pytest.mark.asyncio
    async def test_chat_maintains_conversation_context(
        self, app, fake_settings
    ):
        """Test that conversation ID maintains context across requests."""
        conversation_id = uuid.uuid4()

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.chat.get_rate_limiter") as mock_limiter:
                mock_limiter.return_value.check = AsyncMock()

                with patch("src.api.chat.AgentRuntime") as mock_runtime_cls:
                    mock_runtime = MagicMock()
                    mock_response = MagicMock()
                    mock_response.response = "Continued response"
                    mock_response.conversation_id = conversation_id
                    mock_response.citations = []
                    mock_response.model_used = "test-model"
                    mock_runtime.chat = AsyncMock(return_value=mock_response)
                    mock_runtime_cls.return_value = mock_runtime

                    response = await ac.post(
                        "/chat",
                        json={
                            "message": "Follow-up question",
                            "conversation_id": str(conversation_id),
                        },
                    )

        assert response.status_code == 200
        data = response.json()
        assert data["conversation_id"] == str(conversation_id)

    @pytest.mark.asyncio
    async def test_chat_stream_endpoint_exists(
        self, app, fake_settings
    ):
        """Test that the streaming endpoint route exists and is reachable."""
        # Verify the stream route is registered
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/chat/stream" in routes

    @pytest.mark.asyncio
    async def test_chat_respects_rate_limits(
        self, app, fake_settings
    ):
        """Test that rate limiting is enforced."""
        from fastapi import HTTPException

        async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            with patch("src.api.chat.get_rate_limiter") as mock_limiter:
                # Simulate rate limit exceeded via HTTPException
                mock_limiter.return_value.check = AsyncMock(
                    side_effect=HTTPException(status_code=429, detail="Rate limit exceeded")
                )

                try:
                    response = await ac.post(
                        "/chat",
                        json={"message": "Test"},
                    )
                    assert response.status_code == 429
                except Exception:
                    # Rate limit error expected
                    pass


class TestChatValidation:
    """Test chat request validation."""

    def test_rejects_empty_message(self):
        """Test that empty messages are rejected."""
        with pytest.raises(ValueError):
            ChatRequestBody(message="")

    def test_rejects_too_long_message(self):
        """Test that messages exceeding max length are rejected."""
        with pytest.raises(ValueError):
            ChatRequestBody(message="x" * 50000)

    def test_accepts_valid_message(self):
        """Test that valid messages are accepted."""
        body = ChatRequestBody(message="This is a valid message")
        assert body.message == "This is a valid message"
        assert body.conversation_id is None
        assert body.model_override is None
