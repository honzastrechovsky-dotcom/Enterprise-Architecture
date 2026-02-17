"""API Key model for programmatic access.

API keys provide an alternative to JWT authentication for programmatic access.
Each key is scoped to a tenant and can have specific permissions (scopes) and
rate limits.

Security considerations:
- Only the hash of the key is stored, never the raw key
- Raw keys are only shown once at creation
- Keys can be revoked and rotated
- Keys can have expiration dates
- Last usage is tracked for audit purposes
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class APIKey(Base):
    """API Key for programmatic authentication.

    Instead of JWT tokens, clients can use API keys for service-to-service
    communication or long-running integrations.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Metadata
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable name for the key",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional description of the key's purpose",
    )

    # Security
    key_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        unique=True,
        comment="SHA-256 hash of the raw API key (never store raw key)",
    )
    key_prefix: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="First 8 characters of the key for identification (e.g., 'eap_abcd')",
    )

    # Authorization
    scopes: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        comment="List of allowed scopes (e.g., ['chat', 'documents', 'analytics'])",
    )

    # Rate limiting
    rate_limit_per_minute: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Max requests per minute for this key (None = no limit)",
    )

    # Lifecycle
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who created this API key",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Expiration timestamp (None = never expires)",
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last time this key was used for authentication",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the key is active (can be used for auth)",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the key was revoked (None = not revoked)",
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="api_keys")  # type: ignore[name-defined]
    creator: Mapped[User] = relationship("User")  # type: ignore[name-defined]

    __table_args__ = (
        # Fast lookup by hash for authentication
        Index("ix_api_keys_key_hash", "key_hash"),
        # List keys for a tenant
        Index("ix_api_keys_tenant_id", "tenant_id"),
        # Find active keys
        Index("ix_api_keys_tenant_active", "tenant_id", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<APIKey id={self.id} name={self.name!r} tenant={self.tenant_id} prefix={self.key_prefix!r}>"
