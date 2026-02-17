"""AuditLog model - immutable record of every agent interaction.

Design principles:
- Append-only: never update or delete audit rows
- tenant_id is always set for cross-tenant reporting queries
- Captures enough information for compliance, debugging, and billing:
  model used, tool calls, latency, status, request/response summaries
- Summaries are truncated to avoid storing PII-heavy content at full fidelity
  (raw content lives in the messages table)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class AuditStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Timestamp of when the action was initiated
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )

    # Action identifier, e.g. "chat.message", "document.upload", "admin.create_user"
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # Resource being acted upon
    resource_type: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="e.g. 'conversation', 'document', 'user'",
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="UUID or slug of the resource",
    )

    # LLM call metadata
    model_used: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="LiteLLM model identifier used for this request",
    )
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="List of tool invocations with names and truncated args",
    )

    # Truncated summaries for compliance review (not full content)
    request_summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="First 500 chars of user message",
    )
    response_summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="First 500 chars of assistant response",
    )

    latency_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="End-to-end request latency in milliseconds",
    )
    status: Mapped[AuditStatus] = mapped_column(
        String(32),
        nullable=False,
        default=AuditStatus.SUCCESS,
    )
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Extra structured data (citations used, RAG chunks, etc.)
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    # Relationships (read-only - audit logs are never written via ORM cascade)
    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="audit_logs")  # type: ignore[name-defined]
    user: Mapped[User | None] = relationship("User", back_populates="audit_logs")  # type: ignore[name-defined]

    __table_args__ = (
        Index("ix_audit_tenant_timestamp", "tenant_id", "timestamp"),
        Index("ix_audit_tenant_user", "tenant_id", "user_id"),
        Index("ix_audit_tenant_action", "tenant_id", "action"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} action={self.action!r} status={self.status}>"
        )
