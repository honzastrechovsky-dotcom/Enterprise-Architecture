"""Tenant model - top of the multi-tenant hierarchy.

Every other resource in the system is scoped to a tenant. The tenant
is identified by a UUID that must appear in every JWT and every DB query.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="URL-safe identifier, e.g. 'acme-corp'",
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Soft-delete timestamp; NULL means not deleted
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    users: Mapped[list[User]] = relationship(  # type: ignore[name-defined]
        "User", back_populates="tenant", cascade="all, delete-orphan"
    )
    conversations: Mapped[list[Conversation]] = relationship(  # type: ignore[name-defined]
        "Conversation", back_populates="tenant", cascade="all, delete-orphan"
    )
    documents: Mapped[list[Document]] = relationship(  # type: ignore[name-defined]
        "Document", back_populates="tenant", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(  # type: ignore[name-defined]
        "AuditLog", back_populates="tenant", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list[APIKey]] = relationship(  # type: ignore[name-defined]
        "APIKey", back_populates="tenant", cascade="all, delete-orphan"
    )
    idp_configs: Mapped[list[IdPConfig]] = relationship(  # type: ignore[name-defined]
        "IdPConfig", back_populates="tenant", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} slug={self.slug!r}>"
