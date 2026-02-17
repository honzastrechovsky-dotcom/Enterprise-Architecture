"""Conversation service - business logic for conversation management.

Encapsulates operations on conversations and messages, enforcing
tenant isolation and providing high-level business operations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.policy import apply_tenant_filter
from src.models.conversation import Conversation, Message, MessageRole

log = structlog.get_logger(__name__)


class ConversationService:
    """Service for conversation and message operations."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Conversation:
        """Create a new conversation for a user.

        Args:
            tenant_id: Tenant ID for multi-tenancy scoping
            user_id: User who owns the conversation
            title: Optional conversation title
            metadata: Optional conversation metadata

        Returns:
            Created Conversation instance
        """
        conversation = Conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            title=title,
            metadata_=metadata or {},
            is_archived=False,
        )
        self._db.add(conversation)
        await self._db.flush()

        log.info(
            "conversation.created",
            conversation_id=str(conversation.id),
            tenant_id=str(tenant_id),
            user_id=str(user_id),
        )
        return conversation

    async def list_conversations(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[Conversation]:
        """List conversations for a tenant/user.

        Args:
            tenant_id: Tenant ID for multi-tenancy scoping
            user_id: Optional user ID to filter to specific user's conversations
            limit: Maximum number of conversations to return
            offset: Number of conversations to skip
            include_archived: Whether to include archived conversations

        Returns:
            List of Conversation instances
        """
        stmt = apply_tenant_filter(
            select(Conversation).order_by(Conversation.updated_at.desc()),
            Conversation,
            tenant_id,
        )

        if user_id is not None:
            stmt = stmt.where(Conversation.user_id == user_id)

        if not include_archived:
            stmt = stmt.where(Conversation.is_archived == False)

        stmt = stmt.offset(offset).limit(limit)

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
        include_messages: bool = True,
    ) -> Conversation | None:
        """Get a conversation by ID with tenant scoping.

        Args:
            conversation_id: Conversation ID
            tenant_id: Tenant ID for multi-tenancy scoping
            include_messages: Whether to eagerly load messages

        Returns:
            Conversation instance or None if not found
        """
        stmt = apply_tenant_filter(
            select(Conversation).where(Conversation.id == conversation_id),
            Conversation,
            tenant_id,
        )

        if include_messages:
            stmt = stmt.options(selectinload(Conversation.messages))

        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def add_message(
        self,
        *,
        conversation_id: uuid.UUID,
        role: MessageRole,
        content: str,
        metadata: dict[str, Any] | None = None,
        model_used: str | None = None,
        token_count: int | None = None,
        citations: list[dict[str, Any]] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> Message:
        """Add a message to a conversation.

        Args:
            conversation_id: Conversation to add message to
            role: Message role (user/assistant/system/tool)
            content: Message content
            metadata: Optional message metadata
            model_used: Model that generated this message (for assistant messages)
            token_count: Token count for this message
            citations: Citations from RAG retrieval
            tool_calls: Tool calls made for this message

        Returns:
            Created Message instance

        Raises:
            ValueError: If conversation not found
        """
        # Get conversation to ensure it exists and get tenant_id
        conv_stmt = select(Conversation).where(Conversation.id == conversation_id)
        result = await self._db.execute(conv_stmt)
        conversation = result.scalar_one_or_none()

        if conversation is None:
            raise ValueError(f"Conversation {conversation_id} not found")

        # Get next sequence number
        max_seq_stmt = (
            select(func.coalesce(func.max(Message.sequence_number), 0))
            .where(Message.conversation_id == conversation_id)
        )
        result = await self._db.execute(max_seq_stmt)
        next_seq = result.scalar() + 1

        # Create message
        message = Message(
            conversation_id=conversation_id,
            tenant_id=conversation.tenant_id,
            role=role,
            content=content,
            sequence_number=next_seq,
            model_used=model_used,
            token_count=token_count,
            citations=citations or [],
            tool_calls=tool_calls or [],
        )
        self._db.add(message)

        # Update conversation's updated_at timestamp
        conversation.updated_at = datetime.now(UTC)

        await self._db.flush()

        log.info(
            "message.added",
            message_id=str(message.id),
            conversation_id=str(conversation_id),
            role=role.value,
            sequence=next_seq,
        )
        return message

    async def archive_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
        archived: bool = True,
    ) -> Conversation | None:
        """Archive or unarchive a conversation.

        Args:
            conversation_id: Conversation ID
            tenant_id: Tenant ID for multi-tenancy scoping
            archived: True to archive, False to unarchive

        Returns:
            Updated Conversation instance or None if not found
        """
        conversation = await self.get_conversation(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            include_messages=False,
        )

        if conversation is None:
            return None

        conversation.is_archived = archived
        conversation.updated_at = datetime.now(UTC)
        await self._db.flush()

        log.info(
            "conversation.archived" if archived else "conversation.unarchived",
            conversation_id=str(conversation_id),
            tenant_id=str(tenant_id),
        )
        return conversation

    async def delete_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> bool:
        """Delete a conversation (hard delete with cascade).

        Args:
            conversation_id: Conversation ID
            tenant_id: Tenant ID for multi-tenancy scoping

        Returns:
            True if deleted, False if not found
        """
        conversation = await self.get_conversation(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            include_messages=False,
        )

        if conversation is None:
            return False

        await self._db.delete(conversation)
        await self._db.flush()

        log.info(
            "conversation.deleted",
            conversation_id=str(conversation_id),
            tenant_id=str(tenant_id),
        )
        return True

    async def search_conversations(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None,
        query: str,
        limit: int = 50,
    ) -> list[Conversation]:
        """Search conversations by message content using full-text search.

        Args:
            tenant_id: Tenant ID for multi-tenancy scoping
            user_id: Optional user ID to filter to specific user's conversations
            query: Search query string
            limit: Maximum number of results to return

        Returns:
            List of matching Conversation instances with messages loaded
        """
        # PostgreSQL full-text search on message content
        # Find conversation IDs that have matching messages
        message_stmt = (
            select(Message.conversation_id)
            .where(
                Message.tenant_id == tenant_id,
                or_(
                    Message.content.ilike(f"%{query}%"),
                    # Could add ts_vector based search here for better performance:
                    # func.to_tsvector('english', Message.content).match(query)
                ),
            )
            .distinct()
        )

        result = await self._db.execute(message_stmt)
        matching_conv_ids = [row[0] for row in result.all()]

        if not matching_conv_ids:
            return []

        # Get the conversations
        conv_stmt = apply_tenant_filter(
            select(Conversation)
            .where(Conversation.id.in_(matching_conv_ids))
            .options(selectinload(Conversation.messages))
            .order_by(Conversation.updated_at.desc())
            .limit(limit),
            Conversation,
            tenant_id,
        )

        if user_id is not None:
            conv_stmt = conv_stmt.where(Conversation.user_id == user_id)

        result = await self._db.execute(conv_stmt)
        return list(result.scalars().all())

    async def update_conversation_title(
        self,
        *,
        conversation_id: uuid.UUID,
        tenant_id: uuid.UUID,
        title: str,
    ) -> Conversation | None:
        """Update a conversation's title.

        Args:
            conversation_id: Conversation ID
            tenant_id: Tenant ID for multi-tenancy scoping
            title: New title

        Returns:
            Updated Conversation instance or None if not found
        """
        conversation = await self.get_conversation(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            include_messages=False,
        )

        if conversation is None:
            return None

        conversation.title = title
        conversation.updated_at = datetime.now(UTC)
        await self._db.flush()

        log.info(
            "conversation.title_updated",
            conversation_id=str(conversation_id),
            tenant_id=str(tenant_id),
            title=title,
        )
        return conversation
