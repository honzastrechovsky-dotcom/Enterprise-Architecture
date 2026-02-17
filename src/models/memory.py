"""Per-user memory model.

Memory is a simple key-value store scoped to tenant + user. It provides
persistent context across conversations (e.g., user preferences, extracted
facts, agent notes).

Keys are arbitrary strings. Values are arbitrary JSON. The combination of
(tenant_id, user_id, key) is unique.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    key: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="Memory key, e.g. 'preferred_language', 'last_project'",
    )
    # Store arbitrary JSON values (strings, numbers, objects, arrays)
    value: Mapped[Any] = mapped_column(
        JSONB,
        nullable=False,
        comment="JSON-serializable value",
    )
    # Optional human-readable description of the memory
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="memories")  # type: ignore[name-defined]

    __table_args__ = (
        # Enforce uniqueness of (tenant, user, key)
        Index("ix_memories_tenant_user_key", "tenant_id", "user_id", "key", unique=True),
    )

    def __repr__(self) -> str:
        return f"<Memory id={self.id} user={self.user_id} key={self.key!r}>"
