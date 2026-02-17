"""SQLAlchemy ORM model for GDPR data subject request persistence.

Tracks the full lifecycle of GDPR Article 15 (access), Article 17 (erasure),
and Article 20 (portability) requests, including the mandatory 30-day
compliance deadline.

This model backs GDPRService.get_request_status() and
GDPRService.list_pending_requests() which previously returned stub values.
Migration 016 must be applied before this model is usable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class GDPRRequestRecord(Base):
    """Persistent record of a GDPR data subject rights request."""

    __tablename__ = "gdpr_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="GDPR request primary key",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Owning tenant",
    )
    # No FK to users.id — user row may be anonymised by the erasure request itself
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="Internal user ID of the data subject",
    )
    request_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="access | erasure | portability",
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="pending",
        comment="pending | in_progress | completed | failed | rejected",
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="UTC timestamp of request submission",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp when the request reached a terminal status",
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Compliance deadline — 30 days from requested_at",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Operator notes, rejection reason, or processing comments",
    )
    result_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Serialised export payload for access/portability requests",
    )

    def __repr__(self) -> str:
        return (
            f"<GDPRRequestRecord id={self.id} tenant={self.tenant_id} "
            f"type={self.request_type!r} status={self.status!r}>"
        )
