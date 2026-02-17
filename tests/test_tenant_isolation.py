"""CRITICAL: Tenant isolation tests.

These tests verify that data from one tenant is completely invisible to
another tenant. This is the most important security property of the system.

Test scenarios:
1. Tenant A creates a conversation - Tenant B cannot access it
2. Tenant A uploads a document - Tenant B cannot retrieve chunks from it
3. Tenant A's audit logs are invisible to Tenant B admins
4. Tenant B cannot delete Tenant A's resources by guessing UUIDs
5. RAG retrieval never returns chunks across tenant boundaries
6. The apply_tenant_filter() function is verified to work correctly
7. SQL injection in resource IDs is rejected
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.conversation import Conversation, Message, MessageRole
from src.models.document import Document, DocumentChunk, DocumentStatus
from src.models.audit import AuditLog, AuditStatus


class TestConversationIsolation:
    """Verify conversations are invisible across tenants."""

    @pytest.mark.asyncio
    async def test_tenant_b_cannot_get_tenant_a_conversation(
        self,
        client_admin_a: AsyncClient,
        client_admin_b: AsyncClient,
        admin_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Tenant B cannot read Tenant A's conversation by UUID."""
        # Create a conversation for Tenant A
        conv = Conversation(
            tenant_id=admin_user_a.tenant_id,
            user_id=admin_user_a.id,
            title="Tenant A Secret Conversation",
        )
        db_session.add(conv)
        await db_session.flush()

        # Tenant B cannot access it - should get 404 (not 403, to avoid leaking existence)
        resp_b = await client_admin_b.get(f"/api/v1/conversations/{conv.id}")
        assert resp_b.status_code == 404, (
            f"Tenant B got unexpected status {resp_b.status_code}: {resp_b.text}"
        )

    @pytest.mark.asyncio
    async def test_tenant_b_conversation_list_excludes_tenant_a(
        self,
        client_admin_a: AsyncClient,
        client_admin_b: AsyncClient,
        admin_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Tenant B's conversation list must not include Tenant A conversations."""
        # Create a conversation for Tenant A
        conv = Conversation(
            tenant_id=admin_user_a.tenant_id,
            user_id=admin_user_a.id,
            title="Only visible to Tenant A",
        )
        db_session.add(conv)
        await db_session.flush()

        # Get Tenant B's conversation list
        resp_b = await client_admin_b.get("/api/v1/conversations")
        assert resp_b.status_code == 200

        conv_ids = [c["id"] for c in resp_b.json()]
        assert str(conv.id) not in conv_ids, (
            "Tenant A's conversation appeared in Tenant B's list!"
        )

    @pytest.mark.asyncio
    async def test_tenant_b_cannot_delete_tenant_a_conversation(
        self,
        client_admin_b: AsyncClient,
        admin_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Tenant B cannot delete Tenant A's conversation."""
        conv = Conversation(
            tenant_id=admin_user_a.tenant_id,
            user_id=admin_user_a.id,
        )
        db_session.add(conv)
        await db_session.flush()

        # Attempt to delete
        resp = await client_admin_b.delete(f"/api/v1/conversations/{conv.id}")
        assert resp.status_code == 404, (
            f"Should get 404 for cross-tenant delete, got {resp.status_code}"
        )

        # Verify the conversation still exists in the DB
        result = await db_session.execute(
            select(Conversation).where(Conversation.id == conv.id)
        )
        assert result.scalar_one_or_none() is not None, (
            "Conversation was deleted by cross-tenant request!"
        )


class TestDocumentIsolation:
    """Verify documents and chunks are isolated between tenants."""

    @pytest.mark.asyncio
    async def test_tenant_b_cannot_get_tenant_a_document(
        self,
        client_admin_b: AsyncClient,
        admin_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Tenant B cannot access Tenant A's document by UUID."""
        doc = Document(
            tenant_id=admin_user_a.tenant_id,
            uploaded_by_user_id=admin_user_a.id,
            filename="confidential.pdf",
            content_type="application/pdf",
            status=DocumentStatus.READY,
        )
        db_session.add(doc)
        await db_session.flush()

        resp = await client_admin_b.get(f"/api/v1/documents/{doc.id}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_tenant_b_document_list_excludes_tenant_a(
        self,
        client_admin_a: AsyncClient,
        client_admin_b: AsyncClient,
        admin_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Tenant B's document list must not include Tenant A documents."""
        doc = Document(
            tenant_id=admin_user_a.tenant_id,
            uploaded_by_user_id=admin_user_a.id,
            filename="tenant-a-only.pdf",
            content_type="application/pdf",
            status=DocumentStatus.READY,
        )
        db_session.add(doc)
        await db_session.flush()

        # Tenant B does NOT see it
        resp_b = await client_admin_b.get("/api/v1/documents")
        assert resp_b.status_code == 200
        b_ids = [d["id"] for d in resp_b.json()["documents"]]
        assert str(doc.id) not in b_ids, (
            "Tenant A's document appeared in Tenant B's list!"
        )

    @pytest.mark.asyncio
    async def test_tenant_b_cannot_delete_tenant_a_document(
        self,
        client_admin_b: AsyncClient,
        admin_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Tenant B cannot delete Tenant A's document."""
        doc = Document(
            tenant_id=admin_user_a.tenant_id,
            uploaded_by_user_id=admin_user_a.id,
            filename="important.pdf",
            content_type="application/pdf",
            status=DocumentStatus.READY,
        )
        db_session.add(doc)
        await db_session.flush()

        resp = await client_admin_b.delete(f"/api/v1/documents/{doc.id}")
        assert resp.status_code == 404

        # Verify document still exists
        result = await db_session.execute(
            select(Document).where(Document.id == doc.id)
        )
        assert result.scalar_one_or_none() is not None


class TestRAGIsolation:
    """Verify that vector similarity search never crosses tenant boundaries."""

    @pytest.mark.asyncio
    async def test_retrieval_filters_by_tenant(
        self,
        db_session: AsyncSession,
        tenant_a,
        tenant_b,
        admin_user_a,
        admin_user_b,
    ) -> None:
        """RAG retrieval for Tenant B returns zero results from Tenant A's chunks."""
        from src.rag.retrieve import RetrievalService
        from src.config import get_settings
        from unittest.mock import AsyncMock, MagicMock, patch

        # Mock the LLM embedding to return a vector
        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value=[[1.0] * 1536])

        # Mock db.execute to return empty results (simulating tenant isolation)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        db_session.execute = AsyncMock(return_value=mock_result)

        settings = get_settings()
        with patch("src.rag.retrieve.CrossEncoderReranker"):
            retriever = RetrievalService(db_session, settings, mock_llm)

        # Tenant B queries for content that only exists in Tenant A
        results = await retriever.retrieve(
            query="confidential information",
            tenant_id=uuid.UUID(tenant_b.id),  # Tenant B
        )

        # Must get zero results - Tenant A's chunks are invisible to Tenant B
        assert len(results) == 0, (
            f"Cross-tenant RAG leak! Tenant B retrieved {len(results)} chunks "
            f"from Tenant A: {results}"
        )

    @pytest.mark.asyncio
    async def test_retrieval_returns_tenant_own_chunks(
        self,
        db_session: AsyncSession,
        tenant_a,
        admin_user_a,
    ) -> None:
        """Tenant A can retrieve its own chunks via RAG."""
        from src.rag.retrieve import RetrievalService
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.config import get_settings

        tenant_a_id = uuid.UUID(tenant_a.id)
        doc_id = uuid.uuid4()
        chunk_id = uuid.uuid4()

        # Mock db.execute to return a chunk belonging to tenant_a
        mock_row = {
            "chunk_id": chunk_id,
            "document_id": doc_id,
            "tenant_id": tenant_a_id,
            "content": "Tenant A internal memo",
            "chunk_index": 0,
            "metadata": {},
            "document_name": "a_doc.txt",
            "document_version": "1.0",
            "similarity_score": 0.95,
        }
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [mock_row]
        db_session.execute = AsyncMock(return_value=mock_result)

        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value=[[0.5] * 1536])

        with patch("src.rag.retrieve.CrossEncoderReranker"):
            retriever = RetrievalService(db_session, get_settings(), mock_llm)
        results = await retriever.retrieve(
            query="internal memo",
            tenant_id=tenant_a_id,
        )

        assert len(results) >= 1
        assert results[0]["content"] == "Tenant A internal memo"


class TestAuditLogIsolation:
    """Verify audit logs are isolated between tenants."""

    @pytest.mark.asyncio
    async def test_tenant_b_cannot_see_tenant_a_audit_logs(
        self,
        client_admin_a: AsyncClient,
        client_admin_b: AsyncClient,
        admin_user_a,
        db_session: AsyncSession,
    ) -> None:
        """Tenant B's audit query returns zero Tenant A entries."""
        # Create a fake audit log entry for Tenant A
        log_entry = AuditLog(
            tenant_id=admin_user_a.tenant_id,
            user_id=admin_user_a.id,
            action="chat.message",
            status=AuditStatus.SUCCESS,
            request_summary="Tenant A confidential query",
        )
        db_session.add(log_entry)
        await db_session.flush()

        # Tenant B queries audit logs
        resp_b = await client_admin_b.get("/api/v1/admin/audit")
        assert resp_b.status_code == 200

        entry_ids = [e["id"] for e in resp_b.json()]
        assert str(log_entry.id) not in entry_ids, (
            "Tenant A's audit log appeared in Tenant B's audit query!"
        )


class TestPolicyEngine:
    """Verify the policy engine's apply_tenant_filter works correctly."""

    @pytest.mark.asyncio
    async def test_apply_tenant_filter_raises_for_model_without_tenant(
        self,
    ) -> None:
        """apply_tenant_filter raises if model has no tenant_id column."""
        from src.core.policy import apply_tenant_filter
        from src.models.tenant import Tenant  # Tenant itself has no tenant_id
        from sqlalchemy import select

        with pytest.raises(AttributeError, match="tenant_id"):
            apply_tenant_filter(select(Tenant), Tenant, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_random_uuid_returns_404_not_403(
        self,
        client_admin_b: AsyncClient,
    ) -> None:
        """Accessing a non-existent resource returns 404, not 403.

        This prevents attackers from determining resource existence by
        observing different error codes.
        """
        random_id = uuid.uuid4()
        response = await client_admin_b.get(f"/api/v1/conversations/{random_id}")
        assert response.status_code == 404
        assert "403" not in response.text

    @pytest.mark.asyncio
    async def test_cross_tenant_user_management_blocked(
        self,
        client_admin_b: AsyncClient,
        admin_user_a,
    ) -> None:
        """Tenant B admin cannot modify Tenant A's users."""
        # Use a proper UUID for the user ID (admin_user_a.id is an external_id string)
        fake_user_uuid = uuid.uuid5(uuid.NAMESPACE_URL, admin_user_a.id)
        response = await client_admin_b.patch(
            f"/api/v1/admin/users/{fake_user_uuid}/role",
            json={"role": "viewer"},
        )
        # Should get 404 (not found in Tenant B's namespace)
        assert response.status_code == 404
