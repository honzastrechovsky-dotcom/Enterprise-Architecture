"""Webhook SQLAlchemy models.

Two models:
- Webhook: registered endpoint with event subscriptions
- WebhookDelivery: delivery attempt record with retry state

Webhook and delivery tracking models.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

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


class DeliveryStatus(StrEnum):
    """Lifecycle states of a single webhook delivery attempt."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class Webhook(Base):
    """A tenant-scoped webhook endpoint subscription.

    Stores the target URL, the list of event types to receive, and a
    hashed HMAC secret used to sign each delivery.  The raw secret is
    never persisted; only the bcrypt hash is stored.
    """

    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="HTTPS endpoint that receives event payloads",
    )

    events: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="List of event types this webhook subscribes to",
    )

    secret_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="HMAC-SHA256 key (raw secret is never stored)",
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether deliveries are active for this webhook",
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
    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        "WebhookDelivery",
        back_populates="webhook",
        cascade="all, delete-orphan",
        order_by="WebhookDelivery.created_at.desc()",
    )

    __table_args__ = (
        Index("ix_webhook_tenant_enabled", "tenant_id", "enabled"),
    )

    def __repr__(self) -> str:
        return (
            f"<Webhook id={self.id} tenant={self.tenant_id} "
            f"url={self.url!r} enabled={self.enabled}>"
        )


class WebhookDelivery(Base):
    """A single attempt to deliver an event to a webhook endpoint.

    Tracks HTTP response codes, attempt count, and the timestamp for the
    next exponential-backoff retry.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    webhook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhooks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Event type that triggered this delivery",
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Full event payload delivered to the endpoint",
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DeliveryStatus.PENDING,
        comment="pending | delivered | failed",
    )

    response_code: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="HTTP status code returned by the endpoint",
    )

    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of delivery attempts made",
    )

    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When to attempt the next retry (None if no retry scheduled)",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # Relationships
    webhook: Mapped[Webhook] = relationship("Webhook", back_populates="deliveries")

    __table_args__ = (
        Index("ix_delivery_status_retry", "status", "next_retry_at"),
        Index("ix_delivery_webhook_created", "webhook_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<WebhookDelivery id={self.id} webhook={self.webhook_id} "
            f"event={self.event_type!r} status={self.status} attempts={self.attempts}>"
        )
