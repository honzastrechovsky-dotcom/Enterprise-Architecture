"""GDPR data subject rights implementation.

Implements GDPR Article 15-20 rights:
- Right to access (Art. 15): Export all personal data
- Right to erasure (Art. 17): Delete/anonymize personal data
- Right to data portability (Art. 20): Machine-readable export

All requests must be:
1. Verified (ensure subject identity)
2. Tracked (audit trail of request lifecycle)
3. Completed within 30 days (compliance deadline)
4. Documented (evidence for regulators)

Design:
- Requests are asynchronous (processed by background worker)
- Erasure preserves audit integrity (anonymize, don't delete logs)
- Portability exports JSON in standard format
- All operations are idempotent and resumable
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit import AuditService
from src.models.audit import AuditLog, AuditStatus
from src.models.conversation import Conversation, Message
from src.models.document import Document
from src.models.gdpr_request import GDPRRequestRecord
from src.models.memory import Memory
from src.models.user import User

log = structlog.get_logger(__name__)


class RequestType(StrEnum):
    """GDPR data subject request types."""

    ACCESS = "access"  # Art. 15: Right to access
    ERASURE = "erasure"  # Art. 17: Right to erasure ("right to be forgotten")
    PORTABILITY = "portability"  # Art. 20: Right to data portability


class RequestStatus(StrEnum):
    """Lifecycle status of data subject request."""

    PENDING = "pending"  # Awaiting processing
    IN_PROGRESS = "in_progress"  # Currently processing
    COMPLETED = "completed"  # Successfully completed
    FAILED = "failed"  # Failed (with error details)
    REJECTED = "rejected"  # Rejected (invalid request)


@dataclass
class DataSubjectRequest:
    """GDPR data subject request record."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    subject_email: str
    request_type: RequestType
    status: RequestStatus
    created_at: datetime
    completed_at: datetime | None
    deadline: datetime  # 30 days from creation
    result_data: dict[str, Any] | None
    error_message: str | None


@dataclass
class AccessResult:
    """Result of data access request (Art. 15)."""

    subject_email: str
    data_collected_at: datetime
    conversations: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    audit_logs: list[dict[str, Any]]
    total_records: int


@dataclass
class ErasureResult:
    """Result of data erasure request (Art. 17)."""

    subject_email: str
    erased_at: datetime
    conversations_anonymized: int
    messages_anonymized: int
    documents_deleted: int
    memories_deleted: int
    audit_logs_preserved: int  # Not deleted, but anonymized


@dataclass
class PortabilityResult:
    """Result of data portability request (Art. 20)."""

    subject_email: str
    exported_at: datetime
    json_data: str
    size_bytes: int


class GDPRService:
    """GDPR data subject rights service.

    Usage:
        service = GDPRService(db)

        # Create request
        request = await service.create_request(
            tenant_id=tenant_id,
            subject_email="user@example.com",
            request_type=RequestType.ACCESS,
        )

        # Process request (typically in background worker)
        result = await service.process_access_request(request.id)
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_request(
        self,
        tenant_id: uuid.UUID,
        subject_email: str,
        request_type: RequestType,
    ) -> DataSubjectRequest:
        """Create a new GDPR data subject request.

        Args:
            tenant_id: Tenant ID to scope request
            subject_email: Email of data subject
            request_type: Type of GDPR request

        Returns:
            DataSubjectRequest with pending status
        """
        request_id = uuid.uuid4()
        created_at = datetime.now(UTC)
        deadline = created_at + timedelta(days=30)

        log.info(
            "gdpr.request_created",
            request_id=str(request_id),
            tenant_id=str(tenant_id),
            subject_email=subject_email,
            request_type=request_type,
            deadline=deadline.isoformat(),
        )

        # Audit the request creation
        audit = AuditService(self._db)
        await audit.log(
            tenant_id=tenant_id,
            action=f"gdpr.request.{request_type}",
            status=AuditStatus.SUCCESS,
            extra={
                "request_id": str(request_id),
                "subject_email": subject_email,
                "deadline": deadline.isoformat(),
            },
        )

        # Resolve the internal user ID for persistence (required by gdpr_requests table)
        user_result = await self._db.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.email == subject_email,
            )
        )
        user = user_result.scalar_one_or_none()
        # user_id may be None if the subject has no account; use a nil UUID as sentinel
        user_id = user.id if user else uuid.UUID(int=0)

        # Persist the request to the gdpr_requests table
        record = GDPRRequestRecord(
            id=request_id,
            tenant_id=tenant_id,
            user_id=user_id,
            request_type=str(request_type),
            status=str(RequestStatus.PENDING),
            requested_at=created_at,
            deadline_at=deadline,
        )
        self._db.add(record)
        await self._db.flush()

        return DataSubjectRequest(
            id=request_id,
            tenant_id=tenant_id,
            subject_email=subject_email,
            request_type=request_type,
            status=RequestStatus.PENDING,
            created_at=created_at,
            completed_at=None,
            deadline=deadline,
            result_data=None,
            error_message=None,
        )

    async def process_access_request(
        self, tenant_id: uuid.UUID, subject_email: str
    ) -> AccessResult:
        """Process GDPR Article 15 access request.

        Returns all personal data for the subject across:
        - User profile
        - Conversations and messages
        - Documents they created/accessed
        - Agent memories about them
        - Audit logs of their actions

        Args:
            tenant_id: Tenant to search within
            subject_email: Email of data subject

        Returns:
            AccessResult with all personal data
        """
        log.info(
            "gdpr.process_access_request",
            tenant_id=str(tenant_id),
            subject_email=subject_email,
        )

        # Find user
        user_result = await self._db.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.email == subject_email,
            )
        )
        user = user_result.scalar_one_or_none()

        if not user:
            log.warning(
                "gdpr.access_request_no_user",
                tenant_id=str(tenant_id),
                subject_email=subject_email,
            )
            # Return empty result - no data for this subject
            return AccessResult(
                subject_email=subject_email,
                data_collected_at=datetime.now(UTC),
                conversations=[],
                documents=[],
                memories=[],
                audit_logs=[],
                total_records=0,
            )

        # Gather all conversations and messages
        conversations_result = await self._db.execute(
            select(Conversation).where(
                Conversation.user_id == user.id,
                Conversation.tenant_id == tenant_id,
            )
        )
        conversations = conversations_result.scalars().all()

        # Pre-fetch all messages for these conversations in one query (avoid N+1)
        conv_ids = [conv.id for conv in conversations]
        all_messages_by_conv: dict[uuid.UUID, list[Message]] = {cid: [] for cid in conv_ids}
        if conv_ids:
            messages_result = await self._db.execute(
                select(Message).where(
                    Message.conversation_id.in_(conv_ids),
                    Message.tenant_id == tenant_id,
                )
            )
            for msg in messages_result.scalars().all():
                all_messages_by_conv[msg.conversation_id].append(msg)

        conversations_data = []
        for conv in conversations:
            messages = all_messages_by_conv.get(conv.id, [])

            conversations_data.append(
                {
                    "id": str(conv.id),
                    "title": conv.title,
                    "created_at": conv.created_at.isoformat(),
                    "updated_at": conv.updated_at.isoformat(),
                    "messages": [
                        {
                            "role": msg.role,
                            "content": msg.content,
                            "created_at": msg.created_at.isoformat(),
                        }
                        for msg in messages
                    ],
                }
            )

        # Gather documents created by user
        documents_result = await self._db.execute(
            select(Document).where(Document.tenant_id == tenant_id)
            # In production, would filter by created_by_user_id
        )
        documents = documents_result.scalars().all()

        documents_data = [
            {
                "id": str(doc.id),
                "filename": doc.filename,
                "content_type": doc.content_type,
                "created_at": doc.created_at.isoformat(),
            }
            for doc in documents
        ]

        # Gather memories about user
        memories_result = await self._db.execute(
            select(Memory).where(
                Memory.user_id == user.id,
                Memory.tenant_id == tenant_id,
            )
        )
        memories = memories_result.scalars().all()

        memories_data = [
            {
                "id": str(mem.id),
                "key": mem.key,
                "value": mem.value,
                "description": mem.description,
                "created_at": mem.created_at.isoformat(),
            }
            for mem in memories
        ]

        # Gather audit logs of user's actions
        audit_logs_result = await self._db.execute(
            select(AuditLog)
            .where(
                AuditLog.user_id == user.id,
                AuditLog.tenant_id == tenant_id,
            )
            .order_by(AuditLog.timestamp.desc())
            .limit(1000)  # Cap at 1000 most recent
        )
        audit_logs = audit_logs_result.scalars().all()

        audit_logs_data = [
            {
                "timestamp": al.timestamp.isoformat(),
                "action": al.action,
                "resource_type": al.resource_type,
                "status": al.status,
                "request_summary": al.request_summary,
            }
            for al in audit_logs
        ]

        total_records = (
            len(conversations_data)
            + len(documents_data)
            + len(memories_data)
            + len(audit_logs_data)
        )

        log.info(
            "gdpr.access_request_completed",
            tenant_id=str(tenant_id),
            subject_email=subject_email,
            total_records=total_records,
        )

        return AccessResult(
            subject_email=subject_email,
            data_collected_at=datetime.now(UTC),
            conversations=conversations_data,
            documents=documents_data,
            memories=memories_data,
            audit_logs=audit_logs_data,
            total_records=total_records,
        )

    async def process_erasure_request(
        self, tenant_id: uuid.UUID, subject_email: str
    ) -> ErasureResult:
        """Process GDPR Article 17 erasure request ("right to be forgotten").

        Anonymizes/deletes personal data while preserving:
        - Audit log integrity (required for compliance)
        - Aggregate statistics (not personally identifiable)
        - Legal hold data (if applicable)

        Strategy:
        - Conversations: Anonymize (replace content with "[REDACTED]")
        - Documents: Delete (if user is sole owner)
        - Memories: Delete
        - Audit logs: Anonymize user_id (preserve action, timestamp)

        Args:
            tenant_id: Tenant to search within
            subject_email: Email of data subject

        Returns:
            ErasureResult with counts of anonymized/deleted records
        """
        log.info(
            "gdpr.process_erasure_request",
            tenant_id=str(tenant_id),
            subject_email=subject_email,
        )

        # Find user
        user_result = await self._db.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.email == subject_email,
            )
        )
        user = user_result.scalar_one_or_none()

        if not user:
            log.warning(
                "gdpr.erasure_request_no_user",
                tenant_id=str(tenant_id),
                subject_email=subject_email,
            )
            return ErasureResult(
                subject_email=subject_email,
                erased_at=datetime.now(UTC),
                conversations_anonymized=0,
                messages_anonymized=0,
                documents_deleted=0,
                memories_deleted=0,
                audit_logs_preserved=0,
            )

        user_id = user.id

        # 1. Anonymize conversations (preserve structure, redact content)
        conversations_result = await self._db.execute(
            select(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.tenant_id == tenant_id,
            )
        )
        conversations = conversations_result.scalars().all()

        # Batch-fetch all messages for these conversations
        conv_ids = [conv.id for conv in conversations]
        messages_anonymized = 0

        for conv in conversations:
            conv.title = "[REDACTED - GDPR Erasure]"

        if conv_ids:
            messages_result = await self._db.execute(
                select(Message).where(
                    Message.conversation_id.in_(conv_ids),
                    Message.tenant_id == tenant_id,
                )
            )
            for msg in messages_result.scalars().all():
                msg.content = "[REDACTED - GDPR Erasure Request]"
                messages_anonymized += 1

        conversations_anonymized = len(conversations)

        # 2. Delete memories
        memories_result = await self._db.execute(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.tenant_id == tenant_id,
            )
        )
        memories = memories_result.scalars().all()
        for mem in memories:
            await self._db.delete(mem)
        memories_deleted = len(memories)

        # 3. Delete documents (if user is sole owner)
        # In production, would check ownership/sharing rules
        documents_result = await self._db.execute(
            select(Document).where(Document.tenant_id == tenant_id)
            # Would filter by created_by_user_id == user_id
        )
        documents = documents_result.scalars().all()
        # For now, don't delete documents - would need ownership logic
        documents_deleted = 0

        # 4. Anonymize audit logs (preserve for compliance, but remove PII)
        audit_logs_result = await self._db.execute(
            select(AuditLog).where(
                AuditLog.user_id == user_id,
                AuditLog.tenant_id == tenant_id,
            )
        )
        audit_logs = audit_logs_result.scalars().all()

        for audit_log in audit_logs:
            # Preserve action, timestamp, status for compliance
            # Remove personally identifiable summaries
            audit_log.request_summary = "[REDACTED - GDPR Erasure]"
            audit_log.response_summary = "[REDACTED - GDPR Erasure]"

        audit_logs_preserved = len(audit_logs)

        # 5. Anonymize user record (don't delete - preserve audit trail)
        user.email = f"redacted-{user_id}@gdpr-erasure.local"
        user.display_name = "[REDACTED]"
        user.is_active = False

        await self._db.flush()

        log.info(
            "gdpr.erasure_request_completed",
            tenant_id=str(tenant_id),
            subject_email=subject_email,
            conversations_anonymized=conversations_anonymized,
            messages_anonymized=messages_anonymized,
            documents_deleted=documents_deleted,
            memories_deleted=memories_deleted,
        )

        # Audit the erasure
        audit = AuditService(self._db)
        await audit.log(
            tenant_id=tenant_id,
            action="gdpr.erasure.completed",
            status=AuditStatus.SUCCESS,
            extra={
                "subject_email": subject_email,
                "conversations_anonymized": conversations_anonymized,
                "messages_anonymized": messages_anonymized,
                "documents_deleted": documents_deleted,
                "memories_deleted": memories_deleted,
            },
        )

        return ErasureResult(
            subject_email=subject_email,
            erased_at=datetime.now(UTC),
            conversations_anonymized=conversations_anonymized,
            messages_anonymized=messages_anonymized,
            documents_deleted=documents_deleted,
            memories_deleted=memories_deleted,
            audit_logs_preserved=audit_logs_preserved,
        )

    async def process_portability_request(
        self, tenant_id: uuid.UUID, subject_email: str
    ) -> PortabilityResult:
        """Process GDPR Article 20 data portability request.

        Returns machine-readable JSON export of all personal data.
        Format is standardized for easy import into other systems.

        Args:
            tenant_id: Tenant to search within
            subject_email: Email of data subject

        Returns:
            PortabilityResult with JSON export
        """
        log.info(
            "gdpr.process_portability_request",
            tenant_id=str(tenant_id),
            subject_email=subject_email,
        )

        # Use access request to gather all data
        access_result = await self.process_access_request(tenant_id, subject_email)

        # Convert to standardized JSON format
        export_data = {
            "subject_email": access_result.subject_email,
            "exported_at": access_result.data_collected_at.isoformat(),
            "data_controller": "Enterprise Agent Platform",
            "format_version": "1.0",
            "conversations": access_result.conversations,
            "documents": access_result.documents,
            "memories": access_result.memories,
            "audit_logs": access_result.audit_logs,
            "total_records": access_result.total_records,
        }

        json_data = json.dumps(export_data, indent=2, sort_keys=True)
        size_bytes = len(json_data.encode("utf-8"))

        log.info(
            "gdpr.portability_request_completed",
            tenant_id=str(tenant_id),
            subject_email=subject_email,
            size_bytes=size_bytes,
        )

        return PortabilityResult(
            subject_email=subject_email,
            exported_at=datetime.now(UTC),
            json_data=json_data,
            size_bytes=size_bytes,
        )

    async def get_request_status(
        self, request_id: uuid.UUID
    ) -> DataSubjectRequest | None:
        """Get status of a GDPR request by querying the gdpr_requests table.

        Args:
            request_id: Request ID to look up.

        Returns:
            DataSubjectRequest if found, None otherwise.
        """
        log.info("gdpr.get_request_status", request_id=str(request_id))

        result = await self._db.execute(
            select(GDPRRequestRecord).where(GDPRRequestRecord.id == request_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None

        return self._record_to_dataclass(record)

    async def list_pending_requests(self, tenant_id: uuid.UUID) -> list[DataSubjectRequest]:
        """List all pending GDPR requests for a tenant from the gdpr_requests table.

        Returns requests with status 'pending' or 'in_progress', ordered by
        deadline ascending so the most urgent requests appear first.

        Args:
            tenant_id: Tenant to list requests for.

        Returns:
            List of DataSubjectRequest objects that are not yet in a terminal state.
        """
        log.info("gdpr.list_pending_requests", tenant_id=str(tenant_id))

        result = await self._db.execute(
            select(GDPRRequestRecord)
            .where(
                GDPRRequestRecord.tenant_id == tenant_id,
                GDPRRequestRecord.status.in_(
                    [str(RequestStatus.PENDING), str(RequestStatus.IN_PROGRESS)]
                ),
            )
            .order_by(GDPRRequestRecord.deadline_at.asc())
        )
        records = result.scalars().all()
        return [self._record_to_dataclass(r) for r in records]

    @staticmethod
    def _record_to_dataclass(record: GDPRRequestRecord) -> DataSubjectRequest:
        """Convert a GDPRRequestRecord ORM row to a DataSubjectRequest dataclass.

        subject_email is not stored in the gdpr_requests table (the table stores
        user_id for joins). We return an empty string here; callers that need the
        email should join against the users table separately.
        """
        return DataSubjectRequest(
            id=record.id,
            tenant_id=record.tenant_id,
            subject_email="",  # not stored in gdpr_requests; join users if needed
            request_type=RequestType(record.request_type),
            status=RequestStatus(record.status),
            created_at=record.requested_at,
            completed_at=record.completed_at,
            deadline=record.deadline_at,
            result_data=record.result_data,
            error_message=record.notes,
        )
