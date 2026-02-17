"""User goal persistence model.

Stores long-running user goals so the agent can track progress across
multiple conversations and avoid re-doing completed work.

All reads MUST use apply_tenant_filter() — tenant_id is the isolation boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class UserGoal(Base):
    """Persisted user goal with lifecycle tracking.

    Lifecycle::

        active -> completed
               -> abandoned

    The ``progress_notes`` column accumulates lightweight notes from the
    agent as conversations make progress toward the goal.  Users complete
    goals manually via the API; the agent never auto-completes.
    """

    __tablename__ = "user_goals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="User goal primary key",
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="Owning tenant — all queries must filter on this column",
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="User who owns this goal",
    )

    goal_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Free-text description of the goal",
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="active",
        server_default="active",
        comment="active | completed | abandoned",
    )

    progress_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Accumulated progress notes from agent conversations",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="UTC timestamp of goal creation",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        comment="UTC timestamp of last modification",
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC timestamp when goal was completed",
    )

    __table_args__ = (
        # Primary lookup: all goals for a user within a tenant
        Index("idx_user_goals_tenant_user", "tenant_id", "user_id"),
        # Status filtering across the tenant
        Index("idx_user_goals_status", "status"),
    )

    def __init__(self, **kwargs: Any) -> None:
        """Initialize with Python-level defaults.

        SQLAlchemy mapped_column(default=...) only fires on INSERT, not at
        Python __init__ time.  We set the Python defaults here so that new
        objects are usable immediately after construction, before any DB flush.
        """
        kwargs.setdefault("id", uuid.uuid4())
        kwargs.setdefault("status", "active")
        kwargs.setdefault("created_at", datetime.now(UTC))
        kwargs.setdefault("updated_at", datetime.now(UTC))
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return (
            f"<UserGoal id={self.id} "
            f"tenant={self.tenant_id} "
            f"user={self.user_id} "
            f"status={self.status!r}>"
        )
