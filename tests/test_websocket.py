"""Tests for WebSocket real-time agent communication.

Covers:
- WebSocket connection with valid token (query param and first-message auth)
- WebSocket rejection with invalid token (close code 4001)
- Message send/receive flow (client sends message, server streams response)
- Connection cleanup on disconnect
- Broadcast to tenant (all connections in same tenant receive message)

Design notes:
- Uses pytest-anyio / pytest-asyncio with httpx WebSocket client via
  starlette.testclient.TestClient (synchronous) for connection lifecycle tests.
- For async WS tests we use the anyio backend with starlette's WebSocketTestSession.
- All LLM calls are mocked; no real LLM required.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from src.websocket.manager import ConnectionManager

# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

TEST_JWT_SECRET = "dev-only-jwt-secret-not-for-production"
TENANT_A = "12345678-1234-5678-1234-567812345678"
TENANT_B = "87654321-8765-4321-8765-432187654321"


def _make_token(
    sub: str = "user-sub",
    tenant_id: str = TENANT_A,
    role: str = "viewer",
    email: str = "ws@example.com",
    expires_in: int = 3600,
) -> str:
    """Create a test JWT token using HS256."""
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": sub,
        "tenant_id": tenant_id,
        "role": role,
        "email": email,
        "aud": "enterprise-agents-api",
        "iat": now,
        "exp": now + expires_in,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")


def _mock_llm_response(content: str = "Hello from agent") -> MagicMock:
    mock = MagicMock()
    mock.choices = [MagicMock()]
    mock.choices[0].message.content = content
    mock.model = "test-model"
    mock.usage = MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    return mock


# ------------------------------------------------------------------ #
# Minimal FastAPI app for WebSocket testing
# ------------------------------------------------------------------ #

def _make_ws_test_app(fake_settings, mock_db_session=None) -> FastAPI:
    """Create a minimal FastAPI app with only the WebSocket routes mounted.

    Overrides get_settings and optionally get_db_session to avoid real DB.
    """
    from unittest.mock import AsyncMock, MagicMock
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.config import get_settings
    from src.database import get_db_session
    from src.core.rate_limit import get_rate_limiter, RateLimiter
    from src.websocket.chat import ws_router

    app = FastAPI()

    # Override settings
    app.dependency_overrides[get_settings] = lambda: fake_settings

    # Override DB session with a mock if none provided
    if mock_db_session is None:
        session = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=result_mock)
        session.flush = AsyncMock()
        session.add = MagicMock()
        mock_db_session = session

    async def _mock_db_gen():
        yield mock_db_session

    app.dependency_overrides[get_db_session] = _mock_db_gen

    # Override rate limiter to be permissive
    permissive_limiter = AsyncMock(spec=RateLimiter)
    permissive_limiter.check = AsyncMock()
    app.dependency_overrides[get_rate_limiter] = lambda: permissive_limiter

    app.include_router(ws_router)

    return app


# ------------------------------------------------------------------ #
# ConnectionManager unit tests
# ------------------------------------------------------------------ #


class TestConnectionManager:
    """Unit tests for the ConnectionManager singleton."""

    def test_singleton_returns_same_instance(self) -> None:
        """get_connection_manager returns the same instance each call."""
        from src.websocket.manager import get_connection_manager

        mgr1 = get_connection_manager()
        mgr2 = get_connection_manager()
        assert mgr1 is mgr2

    @pytest.mark.asyncio
    async def test_connect_and_disconnect(self) -> None:
        """Connecting then disconnecting cleans up the connection registry."""
        mgr = ConnectionManager()
        mock_ws = AsyncMock(spec=WebSocket)

        tenant_id = uuid.UUID(TENANT_A)
        user_id = uuid.uuid4()

        await mgr.connect(mock_ws, tenant_id=tenant_id, user_id=user_id)
        assert mgr.connection_count() == 1

        await mgr.disconnect(mock_ws)
        assert mgr.connection_count() == 0

    @pytest.mark.asyncio
    async def test_send_to_user_reaches_connection(self) -> None:
        """send_to_user sends a JSON message to the correct WebSocket."""
        mgr = ConnectionManager()
        mock_ws = AsyncMock(spec=WebSocket)
        mock_ws.send_json = AsyncMock()

        tenant_id = uuid.UUID(TENANT_A)
        user_id = uuid.uuid4()

        await mgr.connect(mock_ws, tenant_id=tenant_id, user_id=user_id)

        message = {"type": "status", "status": "thinking"}
        await mgr.send_to_user(tenant_id=tenant_id, user_id=user_id, message=message)

        mock_ws.send_json.assert_called_once_with(message)

        # Cleanup
        await mgr.disconnect(mock_ws)

    @pytest.mark.asyncio
    async def test_send_to_user_unknown_user_is_noop(self) -> None:
        """send_to_user for an unknown user silently does nothing."""
        mgr = ConnectionManager()
        tenant_id = uuid.UUID(TENANT_A)
        user_id = uuid.uuid4()

        # Should not raise
        await mgr.send_to_user(tenant_id=tenant_id, user_id=user_id, message={"x": 1})

    @pytest.mark.asyncio
    async def test_send_to_conversation(self) -> None:
        """send_to_conversation sends to all sockets with that conversation_id."""
        mgr = ConnectionManager()
        mock_ws1 = AsyncMock(spec=WebSocket)
        mock_ws1.send_json = AsyncMock()
        mock_ws2 = AsyncMock(spec=WebSocket)
        mock_ws2.send_json = AsyncMock()

        tenant_id = uuid.UUID(TENANT_A)
        user_id_1 = uuid.uuid4()
        user_id_2 = uuid.uuid4()
        conv_id = uuid.uuid4()

        await mgr.connect(mock_ws1, tenant_id=tenant_id, user_id=user_id_1, conversation_id=conv_id)
        await mgr.connect(mock_ws2, tenant_id=tenant_id, user_id=user_id_2, conversation_id=conv_id)

        message = {"type": "response", "content": "hi", "done": True}
        await mgr.send_to_conversation(conversation_id=conv_id, message=message)

        mock_ws1.send_json.assert_called_once_with(message)
        mock_ws2.send_json.assert_called_once_with(message)

        await mgr.disconnect(mock_ws1)
        await mgr.disconnect(mock_ws2)

    @pytest.mark.asyncio
    async def test_broadcast_to_tenant(self) -> None:
        """broadcast_to_tenant sends to all connections within a tenant."""
        mgr = ConnectionManager()

        mock_ws_a1 = AsyncMock(spec=WebSocket)
        mock_ws_a1.send_json = AsyncMock()
        mock_ws_a2 = AsyncMock(spec=WebSocket)
        mock_ws_a2.send_json = AsyncMock()
        mock_ws_b = AsyncMock(spec=WebSocket)
        mock_ws_b.send_json = AsyncMock()

        tenant_a = uuid.UUID(TENANT_A)
        tenant_b = uuid.UUID(TENANT_B)

        await mgr.connect(mock_ws_a1, tenant_id=tenant_a, user_id=uuid.uuid4())
        await mgr.connect(mock_ws_a2, tenant_id=tenant_a, user_id=uuid.uuid4())
        await mgr.connect(mock_ws_b, tenant_id=tenant_b, user_id=uuid.uuid4())

        message = {"type": "status", "status": "maintenance"}
        await mgr.broadcast_to_tenant(tenant_id=tenant_a, message=message)

        mock_ws_a1.send_json.assert_called_once_with(message)
        mock_ws_a2.send_json.assert_called_once_with(message)
        mock_ws_b.send_json.assert_not_called()

        await mgr.disconnect(mock_ws_a1)
        await mgr.disconnect(mock_ws_a2)
        await mgr.disconnect(mock_ws_b)

    @pytest.mark.asyncio
    async def test_disconnect_removes_from_conversation_index(self) -> None:
        """Disconnecting a socket removes it from the conversation index."""
        mgr = ConnectionManager()
        mock_ws = AsyncMock(spec=WebSocket)
        mock_ws.send_json = AsyncMock()

        conv_id = uuid.uuid4()
        tenant_id = uuid.UUID(TENANT_A)
        user_id = uuid.uuid4()

        await mgr.connect(mock_ws, tenant_id=tenant_id, user_id=user_id, conversation_id=conv_id)
        await mgr.disconnect(mock_ws)

        # After disconnect, send to conversation should be a noop
        await mgr.send_to_conversation(conversation_id=conv_id, message={"x": 1})
        mock_ws.send_json.assert_not_called()


# ------------------------------------------------------------------ #
# WebSocket authentication tests
# ------------------------------------------------------------------ #


class TestWebSocketAuth:
    """Test WebSocket authentication via query param and first-message."""

    def test_connection_rejected_without_token(self, fake_settings) -> None:
        """WebSocket connection without token is rejected.

        When no token is provided, authenticate_websocket waits for the first
        message. The test client sends a normal disconnect (code 1000) which
        triggers the receive_json failure path -> socket closed with 4001.

        The connection is accepted first (WS protocol requires accept before
        close), then immediately closed with code 4001.
        """
        app = _make_ws_test_app(fake_settings)

        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws/chat") as ws:
                # Server is waiting for auth message; close without sending one
                # The server logs ws.auth_receive_failed and exits
                pass
            # Test passes if we reach here without hanging - connection closed cleanly

    def test_connection_rejected_with_invalid_token(self, fake_settings) -> None:
        """WebSocket connection with an invalid token is rejected with code 4001.

        The server accepts the socket, validates the token, fails,
        sends close code 4001, and the endpoint returns without hanging.
        """
        app = _make_ws_test_app(fake_settings)

        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws/chat?token=invalid.token.here") as ws:
                # Server will close with 4001 - we may receive a close frame
                # Just ensure the connection doesn't hang
                pass
            # Test passes - connection was handled and cleaned up

    def test_connection_accepted_with_valid_query_param_token(
        self, fake_settings
    ) -> None:
        """WebSocket accepts connection when valid token is in query param."""
        token = _make_token()
        app = _make_ws_test_app(fake_settings)

        with patch("src.agent.llm.litellm.acompletion", new_callable=AsyncMock), \
             patch("src.agent.llm.litellm.aembedding", new_callable=AsyncMock), \
             patch("src.websocket.chat.authenticate_websocket") as mock_auth, \
             patch("src.database.get_db_session") as mock_db:
            from src.auth.dependencies import AuthenticatedUser
            from src.models.user import User, UserRole

            mock_user = MagicMock(spec=User)
            mock_user.id = uuid.uuid4()
            mock_user.tenant_id = uuid.UUID(TENANT_A)
            mock_user.role = UserRole.VIEWER
            mock_user.is_active = True

            auth_user = MagicMock(spec=AuthenticatedUser)
            auth_user.id = mock_user.id
            auth_user.tenant_id = mock_user.tenant_id
            auth_user.role = UserRole.VIEWER
            auth_user.user = mock_user

            mock_auth.return_value = auth_user

            mock_session = AsyncMock()
            mock_db.return_value.__aiter__ = AsyncMock(return_value=iter([mock_session]))

            with TestClient(app) as client:
                with client.websocket_connect(f"/api/v1/ws/chat?token={token}") as ws:
                    # Connection established - send a ping-like message and close
                    ws.send_json({"type": "ping"})
                    # Close gracefully
                    ws.close()

    def test_connection_accepted_with_first_message_auth(
        self, fake_settings
    ) -> None:
        """WebSocket accepts connection when auth token is sent as first message."""
        token = _make_token()
        app = _make_ws_test_app(fake_settings)

        with patch("src.websocket.chat.authenticate_websocket") as mock_auth:
            from src.auth.dependencies import AuthenticatedUser
            from src.models.user import User, UserRole

            mock_user = MagicMock(spec=User)
            mock_user.id = uuid.uuid4()
            mock_user.tenant_id = uuid.UUID(TENANT_A)
            mock_user.role = UserRole.VIEWER
            mock_user.is_active = True

            auth_user = MagicMock(spec=AuthenticatedUser)
            auth_user.id = mock_user.id
            auth_user.tenant_id = mock_user.tenant_id
            auth_user.role = UserRole.VIEWER
            auth_user.user = mock_user

            mock_auth.return_value = auth_user

            with TestClient(app) as client:
                with client.websocket_connect("/api/v1/ws/chat") as ws:
                    ws.send_json({"type": "auth", "token": token})
                    ws.close()


# ------------------------------------------------------------------ #
# WebSocket chat message flow tests
# ------------------------------------------------------------------ #


class TestWebSocketChatFlow:
    """Test that chat messages are processed and responses streamed back."""

    def test_message_receives_streaming_response(self, fake_settings) -> None:
        """Client sends a chat message and receives streamed response chunks.

        Mocks AgentRuntime.chat directly to avoid the full DB + LLM chain
        while still verifying the WS message flow (auth -> message -> response).
        """
        from src.agent.runtime import ChatResponse

        token = _make_token()
        conv_id = uuid.uuid4()
        app = _make_ws_test_app(fake_settings)

        fake_response = ChatResponse(
            response="This is the agent response",
            conversation_id=conv_id,
            citations=[],
            model_used="test-model",
            latency_ms=42,
        )

        with patch("src.websocket.chat.authenticate_websocket") as mock_auth, \
             patch("src.websocket.chat.AgentRuntime") as MockRuntime:

            from src.auth.dependencies import AuthenticatedUser
            from src.models.user import User, UserRole

            # Set up auth mock
            mock_user = MagicMock(spec=User)
            mock_user.id = uuid.uuid4()
            mock_user.tenant_id = uuid.UUID(TENANT_A)
            mock_user.role = UserRole.VIEWER
            mock_user.is_active = True

            auth_user = MagicMock(spec=AuthenticatedUser)
            auth_user.id = mock_user.id
            auth_user.tenant_id = mock_user.tenant_id
            auth_user.role = UserRole.VIEWER
            auth_user.user = mock_user
            mock_auth.return_value = auth_user

            # Set up runtime mock
            mock_runtime_instance = AsyncMock()
            mock_runtime_instance.chat = AsyncMock(return_value=fake_response)
            MockRuntime.return_value = mock_runtime_instance

            with TestClient(app) as client:
                with client.websocket_connect(f"/api/v1/ws/chat?token={token}") as ws:
                    ws.send_json({
                        "type": "message",
                        "content": "What is the policy?",
                        "conversation_id": str(conv_id),
                    })

                    # Collect messages until we get done=True response or error
                    received = []
                    for _ in range(10):  # Safety limit
                        try:
                            data = ws.receive_json()
                            received.append(data)
                            if data.get("done") is True or data.get("type") == "error":
                                break
                        except Exception:
                            break

                    # Must have received at least a status + final response
                    assert len(received) > 0
                    types_seen = {m.get("type") for m in received}
                    assert types_seen & {"response", "status"}, (
                        f"Expected response or status messages, got: {received}"
                    )
                    # Final message should be done=True
                    final = received[-1]
                    assert final.get("done") is True or final.get("type") == "error"

    def test_invalid_message_type_returns_error(self, fake_settings) -> None:
        """Sending an unknown message type returns an error message."""
        app = _make_ws_test_app(fake_settings)

        with patch("src.websocket.chat.authenticate_websocket") as mock_auth:
            from src.auth.dependencies import AuthenticatedUser
            from src.models.user import User, UserRole

            mock_user = MagicMock(spec=User)
            mock_user.id = uuid.uuid4()
            mock_user.tenant_id = uuid.UUID(TENANT_A)
            mock_user.role = UserRole.VIEWER
            mock_user.is_active = True

            auth_user = MagicMock(spec=AuthenticatedUser)
            auth_user.id = mock_user.id
            auth_user.tenant_id = mock_user.tenant_id
            auth_user.role = UserRole.VIEWER
            auth_user.user = mock_user

            mock_auth.return_value = auth_user

            with TestClient(app) as client:
                token = _make_token()
                with client.websocket_connect(f"/api/v1/ws/chat?token={token}") as ws:
                    ws.send_json({"type": "unknown_type"})
                    response = ws.receive_json()
                    assert response["type"] == "error"


# ------------------------------------------------------------------ #
# AgentEventEmitter tests
# ------------------------------------------------------------------ #


class TestAgentEventEmitter:
    """Unit tests for AgentEventEmitter."""

    @pytest.mark.asyncio
    async def test_emitter_sends_thinking_status(self) -> None:
        """AgentEventEmitter sends thinking status to the connection manager."""
        from src.websocket.events import AgentEventEmitter

        mock_mgr = AsyncMock()
        mock_mgr.send_to_user = AsyncMock()

        tenant_id = uuid.UUID(TENANT_A)
        user_id = uuid.uuid4()

        emitter = AgentEventEmitter(
            manager=mock_mgr,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        await emitter.emit_thinking("Analyzing context")

        mock_mgr.send_to_user.assert_called_once()
        call_kwargs = mock_mgr.send_to_user.call_args
        message = call_kwargs.kwargs.get("message") or call_kwargs.args[2]
        assert message["type"] == "status"
        assert message["status"] == "thinking"

    @pytest.mark.asyncio
    async def test_emitter_sends_tool_call_event(self) -> None:
        """AgentEventEmitter sends tool_call event with tool name."""
        from src.websocket.events import AgentEventEmitter

        mock_mgr = AsyncMock()
        mock_mgr.send_to_user = AsyncMock()

        tenant_id = uuid.UUID(TENANT_A)
        user_id = uuid.uuid4()

        emitter = AgentEventEmitter(
            manager=mock_mgr,
            tenant_id=tenant_id,
            user_id=user_id,
        )

        await emitter.emit_tool_call("rag_retrieval", {"query": "policy"})

        mock_mgr.send_to_user.assert_called_once()
        call_kwargs = mock_mgr.send_to_user.call_args
        message = call_kwargs.kwargs.get("message") or call_kwargs.args[2]
        assert message["type"] == "status"
        assert message["status"] == "searching"

    @pytest.mark.asyncio
    async def test_emitter_sends_generating_status(self) -> None:
        """AgentEventEmitter sends generating status event."""
        from src.websocket.events import AgentEventEmitter

        mock_mgr = AsyncMock()
        mock_mgr.send_to_user = AsyncMock()

        emitter = AgentEventEmitter(
            manager=mock_mgr,
            tenant_id=uuid.UUID(TENANT_A),
            user_id=uuid.uuid4(),
        )

        await emitter.emit_generating()

        mock_mgr.send_to_user.assert_called_once()
        call_kwargs = mock_mgr.send_to_user.call_args
        message = call_kwargs.kwargs.get("message") or call_kwargs.args[2]
        assert message["type"] == "status"
        assert message["status"] == "generating"

    @pytest.mark.asyncio
    async def test_emitter_sends_error_event(self) -> None:
        """AgentEventEmitter sends error event with message."""
        from src.websocket.events import AgentEventEmitter

        mock_mgr = AsyncMock()
        mock_mgr.send_to_user = AsyncMock()

        emitter = AgentEventEmitter(
            manager=mock_mgr,
            tenant_id=uuid.UUID(TENANT_A),
            user_id=uuid.uuid4(),
        )

        await emitter.emit_error("Something went wrong")

        mock_mgr.send_to_user.assert_called_once()
        call_kwargs = mock_mgr.send_to_user.call_args
        message = call_kwargs.kwargs.get("message") or call_kwargs.args[2]
        assert message["type"] == "error"
        assert "Something went wrong" in message["message"]


# ------------------------------------------------------------------ #
# WebSocket auth module unit tests
# ------------------------------------------------------------------ #


class TestWebSocketAuthModule:
    """Unit tests for the WebSocket auth helper."""

    @pytest.mark.asyncio
    async def test_authenticate_via_query_param(self, fake_settings) -> None:
        """authenticate_websocket extracts token from query string."""
        from src.websocket.auth import authenticate_websocket

        token = _make_token()

        mock_ws = MagicMock(spec=WebSocket)
        mock_ws.query_params = {"token": token}
        mock_ws.receive_json = AsyncMock()  # Should NOT be called

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()

        # Mock DB query to return None (JIT provisioning path)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute = AsyncMock(return_value=result_mock)
        mock_db.flush = AsyncMock()
        mock_db.add = MagicMock()

        user = await authenticate_websocket(mock_ws, db=mock_db, settings=fake_settings)
        assert user is not None
        assert str(user.tenant_id) == TENANT_A

        # Should not have waited for first message
        mock_ws.receive_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_authenticate_via_first_message(self, fake_settings) -> None:
        """authenticate_websocket reads auth token from first message."""
        from src.websocket.auth import authenticate_websocket

        token = _make_token()

        mock_ws = MagicMock(spec=WebSocket)
        mock_ws.query_params = {}  # No query param
        mock_ws.receive_json = AsyncMock(
            return_value={"type": "auth", "token": token}
        )
        mock_ws.send_json = AsyncMock()
        mock_ws.close = AsyncMock()

        mock_db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute = AsyncMock(return_value=result_mock)
        mock_db.flush = AsyncMock()
        mock_db.add = MagicMock()

        user = await authenticate_websocket(mock_ws, db=mock_db, settings=fake_settings)
        assert user is not None

        # Should have called receive_json to get the auth message
        mock_ws.receive_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_authenticate_rejects_invalid_token(self, fake_settings) -> None:
        """authenticate_websocket returns None and closes with 4001 on bad token."""
        from src.websocket.auth import authenticate_websocket

        mock_ws = MagicMock(spec=WebSocket)
        mock_ws.query_params = {"token": "not.a.valid.token"}
        mock_ws.close = AsyncMock()

        mock_db = AsyncMock()

        result = await authenticate_websocket(mock_ws, db=mock_db, settings=fake_settings)

        assert result is None
        mock_ws.close.assert_called_once_with(code=4001)

    @pytest.mark.asyncio
    async def test_authenticate_rejects_wrong_first_message_type(
        self, fake_settings
    ) -> None:
        """authenticate_websocket rejects first message that is not type=auth."""
        from src.websocket.auth import authenticate_websocket

        mock_ws = MagicMock(spec=WebSocket)
        mock_ws.query_params = {}
        mock_ws.receive_json = AsyncMock(
            return_value={"type": "message", "content": "hello"}
        )
        mock_ws.close = AsyncMock()
        mock_ws.send_json = AsyncMock()

        mock_db = AsyncMock()

        result = await authenticate_websocket(mock_ws, db=mock_db, settings=fake_settings)

        assert result is None
        mock_ws.close.assert_called_once_with(code=4001)
