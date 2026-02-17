"""Conversation and Message models.

A Conversation is a thread of messages between a user and the agent.
Messages track role (user/assistant/system) and include metadata about
tool calls and retrieved citations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Conversation(Base):
    __tablename__ = "conversations"

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
    title: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Auto-generated or user-set title for the conversation",
    )
    # Conversation-level metadata (model preference, system prompt override, etc.)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    is_archived: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
        comment="Archived conversations are hidden from default list view",
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
    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="conversations")  # type: ignore[name-defined]
    user: Mapped[User] = relationship("User", back_populates="conversations")  # type: ignore[name-defined]
    messages: Mapped[list[Message]] = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.sequence_number",
    )

    __table_args__ = (
        Index("ix_conversations_tenant_user", "tenant_id", "user_id"),
        Index("ix_conversations_tenant_updated", "tenant_id", "updated_at"),
    )

    def __repr__(self) -> str:
        return f"<Conversation id={self.id} tenant={self.tenant_id}>"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized for query efficiency - every query filters by tenant_id
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="message_role"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Sequence number for ordering within a conversation (monotonically increasing)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Model that generated this message (NULL for user messages)
    model_used: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Token count for this message (NULL if not tracked)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Citations from RAG retrieval (list of citation dicts)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    # Tool calls made to generate this message
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # Relationships
    conversation: Mapped[Conversation] = relationship(
        "Conversation", back_populates="messages"
    )

    __table_args__ = (
        Index("ix_messages_conversation_seq", "conversation_id", "sequence_number"),
        Index("ix_messages_tenant", "tenant_id"),
    )

    def __repr__(self) -> str:
        return f"<Message id={self.id} role={self.role} conv={self.conversation_id}>"
