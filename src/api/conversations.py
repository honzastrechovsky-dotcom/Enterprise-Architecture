"""Conversation history endpoints.

GET    /conversations              - List user's conversations
POST   /conversations              - Create a new conversation
GET    /conversations/{id}         - Get conversation with messages
PATCH  /conversations/{id}         - Update conversation (title, archive)
DELETE /conversations/{id}         - Delete a conversation
POST   /conversations/{id}/messages - Add a message to a conversation
GET    /conversations/search       - Search conversations by content

All conversations are scoped to the authenticated user AND their tenant.
Users can only see their own conversations; admins see all tenant conversations.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.audit import AuditService
from src.core.policy import Permission, apply_tenant_filter, check_permission
from src.database import get_db_session
from src.models.audit import AuditStatus
from src.models.conversation import Conversation, MessageRole
from src.models.user import UserRole
from src.services.conversation import ConversationService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


class MessageResponse(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    sequence_number: int
    model_used: str | None
    citations: list[dict[str, Any]]
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    is_archived: bool
    message_count: int = 0


class ConversationDetail(BaseModel):
    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    is_archived: bool
    messages: list[MessageResponse]


class CreateConversationRequest(BaseModel):
    title: str | None = Field(None, max_length=255, description="Optional conversation title")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Optional metadata")


class UpdateConversationRequest(BaseModel):
    title: str | None = Field(None, max_length=255, description="Update conversation title")
    is_archived: bool | None = Field(None, description="Archive or unarchive conversation")


class AddMessageRequest(BaseModel):
    role: str = Field(..., description="Message role: user, assistant, system, or tool")
    content: str = Field(..., min_length=1, description="Message content")
    model_used: str | None = Field(None, description="Model that generated this message")
    token_count: int | None = Field(None, ge=0, description="Token count for this message")
    citations: list[dict[str, Any]] = Field(default_factory=list, description="Citations")
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, description="Tool calls")


@router.get(
    "",
    response_model=list[ConversationSummary],
    summary="List conversations for the current user",
)
async def list_conversations(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False, description="Include archived conversations"),
) -> list[ConversationSummary]:
    """List conversations. Regular users see only their own; admins see all."""
    check_permission(current_user.role, Permission.CONVERSATION_READ)

    service = ConversationService(db)

    # Non-admins only see their own conversations
    user_id = None if current_user.role == UserRole.ADMIN else current_user.id

    convs = await service.list_conversations(
        tenant_id=current_user.tenant_id,
        user_id=user_id,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
    )

    return [
        ConversationSummary(
            id=c.id,
            title=c.title,
            created_at=c.created_at,
            updated_at=c.updated_at,
            is_archived=c.is_archived,
        )
        for c in convs
    ]


@router.post(
    "",
    response_model=ConversationDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new conversation",
)
async def create_conversation(
    body: CreateConversationRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ConversationDetail:
    """Create a new conversation for the current user."""
    check_permission(current_user.role, Permission.CONVERSATION_WRITE)

    service = ConversationService(db)
    conversation = await service.create_conversation(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        title=body.title,
        metadata=body.metadata,
    )

    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="conversation.create",
        resource_type="conversation",
        resource_id=str(conversation.id),
        status=AuditStatus.SUCCESS,
    )

    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        is_archived=conversation.is_archived,
        messages=[],
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetail,
    summary="Get a conversation with all messages",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ConversationDetail:
    """Get a conversation and its messages (tenant-scoped)."""
    check_permission(current_user.role, Permission.CONVERSATION_READ)

    stmt = (
        apply_tenant_filter(
            select(Conversation)
            .options(selectinload(Conversation.messages))
            .where(Conversation.id == conversation_id),
            Conversation,
            current_user.tenant_id,
        )
    )

    # Non-admins can only access their own conversations
    if current_user.role != UserRole.ADMIN:
        stmt = stmt.where(Conversation.user_id == current_user.id)

    result = await db.execute(stmt)
    conv = result.scalar_one_or_none()

    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    return ConversationDetail(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        is_archived=conv.is_archived,
        messages=[
            MessageResponse(
                id=m.id,
                role=m.role.value,
                content=m.content,
                sequence_number=m.sequence_number,
                model_used=m.model_used,
                citations=m.citations,
                created_at=m.created_at,
            )
            for m in conv.messages
        ],
    )


@router.patch(
    "/{conversation_id}",
    response_model=ConversationDetail,
    summary="Update a conversation",
)
async def update_conversation(
    conversation_id: uuid.UUID,
    body: UpdateConversationRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ConversationDetail:
    """Update conversation title or archive status."""
    check_permission(current_user.role, Permission.CONVERSATION_WRITE)

    service = ConversationService(db)

    # Verify conversation exists and user has access
    conv = await service.get_conversation(
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        include_messages=False,
    )

    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    # Non-admins can only modify their own conversations
    if current_user.role != UserRole.ADMIN and conv.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    # Update title if provided
    if body.title is not None:
        conv = await service.update_conversation_title(
            conversation_id=conversation_id,
            tenant_id=current_user.tenant_id,
            title=body.title,
        )

    # Update archive status if provided
    if body.is_archived is not None:
        conv = await service.archive_conversation(
            conversation_id=conversation_id,
            tenant_id=current_user.tenant_id,
            archived=body.is_archived,
        )

    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    # Reload with messages for response
    conv = await service.get_conversation(
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        include_messages=True,
    )

    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="conversation.update",
        resource_type="conversation",
        resource_id=str(conversation_id),
        status=AuditStatus.SUCCESS,
    )

    return ConversationDetail(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        is_archived=conv.is_archived,
        messages=[
            MessageResponse(
                id=m.id,
                role=m.role.value,
                content=m.content,
                sequence_number=m.sequence_number,
                model_used=m.model_used,
                citations=m.citations,
                created_at=m.created_at,
            )
            for m in conv.messages
        ],
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a message to a conversation",
)
async def add_message(
    conversation_id: uuid.UUID,
    body: AddMessageRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    """Add a message to an existing conversation."""
    check_permission(current_user.role, Permission.CONVERSATION_WRITE)

    service = ConversationService(db)

    # Verify conversation exists and user has access
    conv = await service.get_conversation(
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        include_messages=False,
    )

    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    # Non-admins can only add to their own conversations
    if current_user.role != UserRole.ADMIN and conv.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    # Validate role
    try:
        role = MessageRole(body.role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Must be one of: {', '.join(r.value for r in MessageRole)}",
        )

    # Add message
    try:
        message = await service.add_message(
            conversation_id=conversation_id,
            role=role,
            content=body.content,
            model_used=body.model_used,
            token_count=body.token_count,
            citations=body.citations,
            tool_calls=body.tool_calls,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="message.create",
        resource_type="message",
        resource_id=str(message.id),
        status=AuditStatus.SUCCESS,
    )

    return MessageResponse(
        id=message.id,
        role=message.role.value,
        content=message.content,
        sequence_number=message.sequence_number,
        model_used=message.model_used,
        citations=message.citations,
        created_at=message.created_at,
    )


@router.get(
    "/search",
    response_model=list[ConversationDetail],
    summary="Search conversations by content",
)
async def search_conversations(
    q: str = Query(..., min_length=1, max_length=500, description="Search query"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=100),
) -> list[ConversationDetail]:
    """Search conversations by message content. Regular users search only their own."""
    check_permission(current_user.role, Permission.CONVERSATION_READ)

    service = ConversationService(db)

    # Non-admins only search their own conversations
    user_id = None if current_user.role == UserRole.ADMIN else current_user.id

    conversations = await service.search_conversations(
        tenant_id=current_user.tenant_id,
        user_id=user_id,
        query=q,
        limit=limit,
    )

    return [
        ConversationDetail(
            id=c.id,
            title=c.title,
            created_at=c.created_at,
            updated_at=c.updated_at,
            is_archived=c.is_archived,
            messages=[
                MessageResponse(
                    id=m.id,
                    role=m.role.value,
                    content=m.content,
                    sequence_number=m.sequence_number,
                    model_used=m.model_used,
                    citations=m.citations,
                    created_at=m.created_at,
                )
                for m in c.messages
            ],
        )
        for c in conversations
    ]


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a conversation",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a conversation and all its messages."""
    check_permission(current_user.role, Permission.CONVERSATION_DELETE)

    service = ConversationService(db)

    # Verify conversation exists and user has access
    conv = await service.get_conversation(
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
        include_messages=False,
    )

    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    # Non-admins can only delete their own conversations
    if current_user.role != UserRole.ADMIN and conv.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    deleted = await service.delete_conversation(
        conversation_id=conversation_id,
        tenant_id=current_user.tenant_id,
    )

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    audit = AuditService(db)
    await audit.log(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        action="conversation.delete",
        resource_type="conversation",
        resource_id=str(conversation_id),
        status=AuditStatus.SUCCESS,
    )
