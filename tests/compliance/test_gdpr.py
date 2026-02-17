"""Tests for GDPR data subject rights implementation."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.compliance.gdpr import (
    GDPRService,
    RequestType,
    AccessResult,
    ErasureResult,
    PortabilityResult,
)
from src.models.user import User, UserRole
from src.models.conversation import Conversation, Message
from src.models.document import Document
from src.models.memory import Memory
from src.models.audit import AuditLog


class TestGDPRService:
    """Test GDPR data subject rights service."""

    @pytest.fixture
    def service(self, mock_db_session):
        """Create GDPRService instance."""
        return GDPRService(mock_db_session)

    @pytest.mark.asyncio
    async def test_process_access_request_returns_all_personal_data(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test Article 15 - Right to access returns complete data export."""
        subject_email = "user@example.com"

        # Mock user lookup
        user = User(
            id=uuid.uuid4(),
            tenant_id=test_tenant_id,
            email=subject_email,
            display_name="Test User",
            role=UserRole.VIEWER,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = user

        # Mock conversations
        conv_result = MagicMock()
        conv_result.scalars.return_value.all.return_value = []

        # Mock documents
        doc_result = MagicMock()
        doc_result.scalars.return_value.all.return_value = []

        # Mock memories
        mem_result = MagicMock()
        mem_result.scalars.return_value.all.return_value = []

        # Mock audit logs
        audit_result = MagicMock()
        audit_result.scalars.return_value.all.return_value = []

        mock_db_session.execute.side_effect = [
            user_result,
            conv_result,
            doc_result,
            mem_result,
            audit_result,
        ]

        result = await service.process_access_request(test_tenant_id, subject_email)

        assert isinstance(result, AccessResult)
        assert result.subject_email == subject_email
        assert isinstance(result.conversations, list)
        assert isinstance(result.documents, list)
        assert isinstance(result.memories, list)
        assert isinstance(result.audit_logs, list)

    @pytest.mark.asyncio
    async def test_process_access_request_returns_empty_for_nonexistent_user(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test access request for non-existent user returns empty result."""
        subject_email = "nonexistent@example.com"

        # Mock user not found
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = None
        mock_db_session.execute.return_value = user_result

        result = await service.process_access_request(test_tenant_id, subject_email)

        assert isinstance(result, AccessResult)
        assert result.subject_email == subject_email
        assert result.total_records == 0
        assert len(result.conversations) == 0
        assert len(result.documents) == 0

    @pytest.mark.asyncio
    async def test_process_erasure_request_anonymizes_data(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test Article 17 - Right to erasure anonymizes personal data."""
        subject_email = "user@example.com"
        user_id = uuid.uuid4()

        # Mock user lookup
        user = User(
            id=user_id,
            tenant_id=test_tenant_id,
            email=subject_email,
            display_name="Test User",
            role=UserRole.VIEWER,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = user

        # Mock conversations with messages
        conversation = MagicMock()
        conversation.id = uuid.uuid4()
        conversation.title = "Original Title"

        conv_result = MagicMock()
        conv_result.scalars.return_value.all.return_value = [conversation]

        # Mock messages
        message = MagicMock()
        message.content = "Original content"

        msg_result = MagicMock()
        msg_result.scalars.return_value.all.return_value = [message]

        # Mock memories, documents, audit logs
        mem_result = MagicMock()
        mem_result.scalars.return_value.all.return_value = []

        doc_result = MagicMock()
        doc_result.scalars.return_value.all.return_value = []

        audit_result = MagicMock()
        audit_result.scalars.return_value.all.return_value = []

        mock_db_session.execute.side_effect = [
            user_result,
            conv_result,
            msg_result,
            mem_result,
            doc_result,
            audit_result,
        ]

        result = await service.process_erasure_request(test_tenant_id, subject_email)

        assert isinstance(result, ErasureResult)
        assert result.subject_email == subject_email
        assert result.conversations_anonymized >= 0
        assert result.messages_anonymized >= 0

        # Verify anonymization occurred
        assert conversation.title == "[REDACTED - GDPR Erasure]"
        assert message.content == "[REDACTED - GDPR Erasure Request]"
        assert user.is_active is False

    @pytest.mark.asyncio
    async def test_process_erasure_request_preserves_audit_logs(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test erasure preserves audit log integrity while removing PII."""
        subject_email = "user@example.com"
        user_id = uuid.uuid4()

        user = User(
            id=user_id,
            tenant_id=test_tenant_id,
            email=subject_email,
            display_name="Test User",
            role=UserRole.VIEWER,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = user

        # Mock audit logs
        audit_log = MagicMock()
        audit_log.request_summary = "User action details"
        audit_log.response_summary = "Response details"

        audit_result = MagicMock()
        audit_result.scalars.return_value.all.return_value = [audit_log]

        # Mock empty results for other queries
        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []

        mock_db_session.execute.side_effect = [
            user_result,
            empty_result,  # conversations
            empty_result,  # memories
            empty_result,  # documents
            audit_result,
        ]

        result = await service.process_erasure_request(test_tenant_id, subject_email)

        # Audit logs should be preserved but anonymized
        assert result.audit_logs_preserved == 1
        assert audit_log.request_summary == "[REDACTED - GDPR Erasure]"
        assert audit_log.response_summary == "[REDACTED - GDPR Erasure]"

    @pytest.mark.asyncio
    async def test_process_portability_request_returns_json_export(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test Article 20 - Right to data portability returns machine-readable JSON."""
        subject_email = "user@example.com"

        user = User(
            id=uuid.uuid4(),
            tenant_id=test_tenant_id,
            email=subject_email,
            display_name="Test User",
            role=UserRole.VIEWER,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = user

        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []

        mock_db_session.execute.side_effect = [
            user_result,
            empty_result,  # conversations
            empty_result,  # documents
            empty_result,  # memories
            empty_result,  # audit logs
        ]

        result = await service.process_portability_request(test_tenant_id, subject_email)

        assert isinstance(result, PortabilityResult)
        assert result.subject_email == subject_email
        assert isinstance(result.json_data, str)
        assert result.size_bytes > 0

        # Verify JSON is valid
        import json
        data = json.loads(result.json_data)
        assert data["subject_email"] == subject_email
        assert "conversations" in data
        assert "documents" in data
        assert "format_version" in data

    @pytest.mark.asyncio
    async def test_create_request_sets_30_day_deadline(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test GDPR request has 30-day completion deadline."""
        from datetime import timedelta

        request = await service.create_request(
            tenant_id=test_tenant_id,
            subject_email="user@example.com",
            request_type=RequestType.ACCESS,
        )

        # Verify 30-day deadline
        deadline_delta = request.deadline - request.created_at
        assert 29 <= deadline_delta.days <= 31  # Allow for time precision

    @pytest.mark.asyncio
    async def test_erasure_request_anonymizes_user_email(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test erasure anonymizes user email to prevent re-identification."""
        subject_email = "user@example.com"
        user_id = uuid.uuid4()

        user = User(
            id=user_id,
            tenant_id=test_tenant_id,
            email=subject_email,
            display_name="Test User",
            role=UserRole.VIEWER,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = user

        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []

        mock_db_session.execute.side_effect = [
            user_result,
            empty_result,  # conversations
            empty_result,  # memories
            empty_result,  # documents
            empty_result,  # audit logs
        ]

        await service.process_erasure_request(test_tenant_id, subject_email)

        # Verify email anonymization
        assert user.email != subject_email
        assert "redacted" in user.email
        assert "@gdpr-erasure.local" in user.email
        assert user.display_name == "[REDACTED]"
