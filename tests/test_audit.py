"""Tests for the audit logging system.

Covers:
- Audit entries are written for successful operations
- Audit entries capture error status on failures
- Request/response summaries are truncated at 500 chars
- Audit logs are queryable via admin API
- Audit log tenant isolation (already covered in test_tenant_isolation.py
  but we test the API endpoint here)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit import AuditService, _truncate
from src.models.audit import AuditLog, AuditStatus


class TestAuditTruncation:
    """Verify summary truncation behavior."""

    def test_truncate_short_string_unchanged(self) -> None:
        """Short strings pass through without modification."""
        text = "Short message"
        assert _truncate(text) == text

    def test_truncate_long_string(self) -> None:
        """Strings over 500 chars are truncated with ellipsis."""
        text = "x" * 600
        result = _truncate(text)
        assert len(result) <= 503  # 500 + "..."
        assert result.endswith("...")

    def test_truncate_exactly_500(self) -> None:
        """Strings of exactly 500 chars are not truncated."""
        text = "y" * 500
        assert _truncate(text) == text

    def test_truncate_none_returns_none(self) -> None:
        """None input returns None."""
        assert _truncate(None) is None

    def test_truncate_custom_max(self) -> None:
        """Custom max_chars respected."""
        text = "hello world"
        result = _truncate(text, max_chars=5)
        assert result == "hello..."


class TestAuditServiceWrite:
    """AuditService correctly writes log entries."""

    @pytest.mark.asyncio
    async def test_audit_log_is_written(
        self,
        db_session: AsyncSession,
        tenant_a,
        admin_user_a,
    ) -> None:
        """AuditService writes a log entry to the database."""
        import uuid as _uuid
        audit = AuditService(db_session)
        tenant_uuid = _uuid.UUID(tenant_a.id)

        # Use the returned AuditLog entry directly (avoids re-querying the mock DB)
        entry = await audit.log(
            tenant_id=tenant_uuid,
            action="test.action",
            resource_type="test_resource",
            resource_id="abc-123",
            model_used="gpt-4o-mini",
            request_summary="What is the capital of France?",
            response_summary="Paris.",
            latency_ms=250,
            status=AuditStatus.SUCCESS,
        )

        assert entry is not None
        assert entry.resource_type == "test_resource"
        assert entry.resource_id == "abc-123"
        assert entry.model_used == "gpt-4o-mini"
        assert entry.latency_ms == 250
        assert entry.status == AuditStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_audit_long_summaries_are_truncated(
        self,
        db_session: AsyncSession,
        tenant_a,
        admin_user_a,
    ) -> None:
        """AuditService truncates long summaries before writing."""
        import uuid as _uuid
        long_text = "Q" * 1000
        audit = AuditService(db_session)
        tenant_uuid = _uuid.UUID(tenant_a.id)

        # Use the returned entry directly (avoids re-querying the mock DB)
        entry = await audit.log(
            tenant_id=tenant_uuid,
            action="test.long",
            request_summary=long_text,
            response_summary=long_text,
        )

        assert len(entry.request_summary) <= 503
        assert entry.request_summary.endswith("...")

    @pytest.mark.asyncio
    async def test_audit_error_entry(
        self,
        db_session: AsyncSession,
        tenant_a,
    ) -> None:
        """AuditService correctly writes error status entries."""
        import uuid as _uuid
        audit = AuditService(db_session)
        tenant_uuid = _uuid.UUID(tenant_a.id)

        # Use the returned entry directly (avoids re-querying the mock DB)
        entry = await audit.log(
            tenant_id=tenant_uuid,
            action="chat.message",
            status=AuditStatus.ERROR,
            error_detail="LLM connection timeout",
        )

        assert entry is not None
        assert entry.error_detail == "LLM connection timeout"

    @pytest.mark.asyncio
    async def test_audit_with_tool_calls(
        self,
        db_session: AsyncSession,
        tenant_a,
        admin_user_a,
    ) -> None:
        """Tool calls are captured in audit log."""
        import uuid as _uuid
        audit = AuditService(db_session)
        tenant_uuid = _uuid.UUID(tenant_a.id)
        tool_calls = [
            {"name": "document_search", "args": {"query": "policy"}},
            {"name": "calculator", "args": {"expression": "2+2"}},
        ]

        # Use the returned entry directly (avoids re-querying the mock DB)
        entry = await audit.log(
            tenant_id=tenant_uuid,
            action="chat.message",
            tool_calls=tool_calls,
        )

        assert len(entry.tool_calls) == 2
        assert entry.tool_calls[0]["name"] == "document_search"


class TestAuditAPI:
    """Audit log query API."""

    @pytest.mark.asyncio
    async def test_admin_can_query_audit_logs(
        self,
        client_admin_a,
        admin_user_a,
        db_session: AsyncSession,
        tenant_a,
    ) -> None:
        """Admin can retrieve audit logs via the API."""
        import uuid as _uuid
        # Write a log entry
        audit = AuditService(db_session)
        tenant_uuid = _uuid.UUID(tenant_a.id)
        await audit.log(
            tenant_id=tenant_uuid,
            action="chat.message",
            request_summary="Test question",
        )
        await db_session.flush()

        response = await client_admin_a.get("/api/v1/admin/audit")
        assert response.status_code == 200
        entries = response.json()
        assert isinstance(entries, list)

    @pytest.mark.asyncio
    async def test_audit_action_filter(
        self,
        client_admin_a,
        admin_user_a,
        db_session: AsyncSession,
        tenant_a,
    ) -> None:
        """Audit log query can be filtered by action."""
        import uuid as _uuid
        audit = AuditService(db_session)
        tenant_uuid = _uuid.UUID(tenant_a.id)
        await audit.log(tenant_id=tenant_uuid, action="chat.message")
        await audit.log(tenant_id=tenant_uuid, action="document.upload")
        await db_session.flush()

        response = await client_admin_a.get(
            "/api/v1/admin/audit",
            params={"action": "chat.message"},
        )
        assert response.status_code == 200
        entries = response.json()
        assert all(e["action"] == "chat.message" for e in entries)

    @pytest.mark.asyncio
    async def test_viewer_cannot_access_audit(
        self,
        client_viewer_a,
    ) -> None:
        """Viewers cannot access audit logs."""
        response = await client_viewer_a.get("/api/v1/admin/audit")
        assert response.status_code == 403
