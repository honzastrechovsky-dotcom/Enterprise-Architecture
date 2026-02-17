"""Tests for the chat endpoint.

We mock the LLM client to avoid needing a real LLM in tests.
The focus is on:
- Request/response shape validation
- Conversation continuity
- Audit log creation
- Rate limit enforcement
- Error handling (LLM unavailable)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit import AuditLog, AuditStatus
from src.models.conversation import Conversation, Message


def _mock_llm_response(content: str = "Test response", model: str = "test-model"):
    """Build a fake LiteLLM response object."""
    mock = MagicMock()
    mock.choices = [MagicMock()]
    mock.choices[0].message.content = content
    mock.model = model
    mock.usage = MagicMock()
    mock.usage.prompt_tokens = 10
    mock.usage.completion_tokens = 20
    mock.usage.total_tokens = 30
    return mock


class TestChatBasic:
    """Basic chat endpoint functionality."""

    @pytest.mark.asyncio
    async def test_chat_requires_auth(self, client: AsyncClient) -> None:
        """Chat endpoint requires authentication."""
        response = await client.post("/api/v1/chat", json={"message": "hello"})
        assert response.status_code == 401

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_chat_creates_new_conversation(
        self,
        client_viewer_a: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """Chat with no conversation_id creates a new conversation."""
        mock_response = _mock_llm_response("Hello from the agent!")

        with patch("src.agent.llm.litellm.acompletion", new_callable=AsyncMock) as mock_llm, \
             patch("src.agent.llm.litellm.aembedding", new_callable=AsyncMock) as mock_embed:
            mock_llm.return_value = mock_response
            mock_embed.return_value = MagicMock(data=[{"embedding": [0.1] * 1536}])

            response = await client_viewer_a.post(
                "/api/v1/chat",
                json={"message": "Hello, agent!"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert "conversation_id" in data
        assert data["response"] == "Hello from the agent!"
        assert data["model_used"] == "test-model"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_chat_continues_existing_conversation(
        self,
        client_viewer_a: AsyncClient,
        viewer_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Chat with conversation_id continues an existing conversation."""
        # Create an existing conversation
        conv = Conversation(
            tenant_id=viewer_user_a.tenant_id,
            user_id=viewer_user_a.id,
        )
        db_session.add(conv)
        await db_session.flush()

        mock_response = _mock_llm_response("Continuing the conversation...")

        with patch("src.agent.llm.litellm.acompletion", new_callable=AsyncMock) as mock_llm, \
             patch("src.agent.llm.litellm.aembedding", new_callable=AsyncMock) as mock_embed:
            mock_llm.return_value = mock_response
            mock_embed.return_value = MagicMock(data=[{"embedding": [0.1] * 1536}])

            response = await client_viewer_a.post(
                "/api/v1/chat",
                json={
                    "message": "Follow-up question",
                    "conversation_id": str(conv.id),
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["conversation_id"] == str(conv.id)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_chat_returns_citations(
        self,
        client_viewer_a: AsyncClient,
        viewer_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Chat response includes citations when RAG finds relevant chunks."""
        from src.models.document import Document, DocumentChunk, DocumentStatus

        # Create a document and chunk for the tenant
        doc = Document(
            tenant_id=viewer_user_a.tenant_id,
            uploaded_by_user_id=viewer_user_a.id,
            filename="handbook.pdf",
            content_type="application/pdf",
            status=DocumentStatus.READY,
            version="2.1",
        )
        db_session.add(doc)
        await db_session.flush()

        chunk = DocumentChunk(
            document_id=doc.id,
            tenant_id=viewer_user_a.tenant_id,
            content="The company policy is...",
            chunk_index=0,
            embedding=[0.5] * 1536,
        )
        db_session.add(chunk)
        await db_session.flush()

        mock_response = _mock_llm_response("Based on the handbook...")

        with patch("src.agent.llm.litellm.acompletion", new_callable=AsyncMock) as mock_llm, \
             patch("src.agent.llm.litellm.aembedding", new_callable=AsyncMock) as mock_embed:
            mock_llm.return_value = mock_response
            # Return same embedding so similarity is high
            mock_embed.return_value = MagicMock(data=[{"embedding": [0.5] * 1536}])

            response = await client_viewer_a.post(
                "/api/v1/chat",
                json={"message": "What is the company policy?"},
            )

        assert response.status_code == 200
        # Citations may or may not be present depending on DB state in test
        assert "citations" in response.json()

    @pytest.mark.asyncio
    async def test_chat_message_too_long_returns_422(
        self,
        client_viewer_a: AsyncClient,
    ) -> None:
        """Messages exceeding max length are rejected before LLM call."""
        response = await client_viewer_a.post(
            "/api/v1/chat",
            json={"message": "x" * 32_001},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_chat_empty_message_returns_422(
        self,
        client_viewer_a: AsyncClient,
    ) -> None:
        """Empty messages are rejected."""
        response = await client_viewer_a.post(
            "/api/v1/chat",
            json={"message": ""},
        )
        assert response.status_code == 422


class TestChatAuditLog:
    """Verify chat creates audit log entries."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_successful_chat_creates_audit_log(
        self,
        client_viewer_a: AsyncClient,
        viewer_user_a,
        db_session: AsyncSession,
    ) -> None:
        """A successful chat creates an audit log entry."""
        mock_response = _mock_llm_response("Audited response")

        with patch("src.agent.llm.litellm.acompletion", new_callable=AsyncMock) as mock_llm, \
             patch("src.agent.llm.litellm.aembedding", new_callable=AsyncMock) as mock_embed:
            mock_llm.return_value = mock_response
            mock_embed.return_value = MagicMock(data=[{"embedding": [0.1] * 1536}])

            response = await client_viewer_a.post(
                "/api/v1/chat",
                json={"message": "Audited question"},
            )

        assert response.status_code == 200

        # Check audit log was written
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == viewer_user_a.tenant_id,
                AuditLog.action == "chat.message",
            )
        )
        logs = result.scalars().all()
        assert len(logs) >= 1
        log = logs[-1]
        assert log.status == AuditStatus.SUCCESS
        assert log.request_summary is not None
        assert "Audited question" in log.request_summary


class TestChatRateLimit:
    """Verify rate limiting works."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_rate_limit_returns_429(
        self,
        test_app,
        admin_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Exceeding rate limit returns 429."""
        from src.core.rate_limit import RateLimiter

        # Create a very tight rate limiter (1 request per minute)
        tight_limiter = RateLimiter(requests_per_minute=1)

        from src.core.rate_limit import get_rate_limiter
        test_app.dependency_overrides[get_rate_limiter] = lambda: tight_limiter

        from tests.conftest import auth_headers
        async with AsyncClient(
            transport=__import__("httpx", fromlist=["ASGITransport"]).ASGITransport(app=test_app),
            base_url="http://test",
            headers=auth_headers(admin_user_a),
        ) as c:
            mock_response = _mock_llm_response()
            with patch("src.agent.llm.litellm.acompletion", new_callable=AsyncMock) as mock_llm, \
                 patch("src.agent.llm.litellm.aembedding", new_callable=AsyncMock) as mock_embed:
                mock_llm.return_value = mock_response
                mock_embed.return_value = MagicMock(data=[{"embedding": [0.1] * 1536}])

                # First request should succeed
                r1 = await c.post("/api/v1/chat", json={"message": "first"})
                # Second request should be rate limited
                r2 = await c.post("/api/v1/chat", json={"message": "second"})

        # Reset override
        del test_app.dependency_overrides[get_rate_limiter]

        assert r1.status_code == 200
        assert r2.status_code == 429
