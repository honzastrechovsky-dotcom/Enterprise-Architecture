"""API routes for shared spaces.

Endpoints:
- POST /spaces - Create a shared space
- GET /spaces - List user's spaces
- POST /spaces/{id}/members - Add member to space
- DELETE /spaces/{id}/members/{user_id} - Remove member from space
- POST /spaces/{id}/share - Share conversation to space
- GET /spaces/{id}/conversations - List shared conversations in space

All endpoints enforce tenant isolation and RBAC.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.policy import require_role
from src.database import get_db_session
from src.models.user import UserRole
from src.scale.shared_spaces import (
    SharedConversation,
    SharedSpace,
    SharedSpaceService,
    SpaceRole,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/spaces", tags=["spaces"])


# ------------------------------------------------------------------ #
# Request/Response models
# ------------------------------------------------------------------ #


class CreateSpaceRequest(BaseModel):
    """Request to create a shared space."""

    name: str = Field(..., min_length=1, max_length=255, description="Space name")
    description: str | None = Field(None, max_length=1000, description="Space description")


class AddMemberRequest(BaseModel):
    """Request to add a member to a space."""

    user_id: str = Field(..., description="User UUID to add")
    role: SpaceRole = Field(..., description="Role to assign")


class ShareConversationRequest(BaseModel):
    """Request to share a conversation to a space."""

    conversation_id: str = Field(..., description="Conversation UUID to share")


class SpaceMemberResponse(BaseModel):
    """Response model for a space member."""

    user_id: str
    role: str
    added_at: str
    added_by: str


class SharedSpaceResponse(BaseModel):
    """Response model for a shared space."""

    id: str
    tenant_id: str
    name: str
    description: str | None
    created_by: str
    created_at: str
    members: list[SpaceMemberResponse]

    @classmethod
    def from_space(cls, space: SharedSpace) -> SharedSpaceResponse:
        """Convert SharedSpace to response model.

        Args:
            space: SharedSpace object

        Returns:
            SharedSpaceResponse
        """
        return cls(
            id=str(space.id),
            tenant_id=str(space.tenant_id),
            name=space.name,
            description=space.description,
            created_by=str(space.created_by),
            created_at=space.created_at.isoformat(),
            members=[
                SpaceMemberResponse(
                    user_id=str(m.user_id),
                    role=m.role,
                    added_at=m.added_at.isoformat(),
                    added_by=str(m.added_by),
                )
                for m in space.members
            ],
        )


class SharedConversationResponse(BaseModel):
    """Response model for a shared conversation."""

    conversation_id: str
    space_id: str
    shared_by: str
    shared_at: str

    @classmethod
    def from_shared_conversation(cls, sc: SharedConversation) -> SharedConversationResponse:
        """Convert SharedConversation to response model.

        Args:
            sc: SharedConversation object

        Returns:
            SharedConversationResponse
        """
        return cls(
            conversation_id=str(sc.conversation_id),
            space_id=str(sc.space_id),
            shared_by=str(sc.shared_by),
            shared_at=sc.shared_at.isoformat(),
        )


# ------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------ #


@router.post("", response_model=SharedSpaceResponse, status_code=status.HTTP_201_CREATED)
async def create_space(
    request: CreateSpaceRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> SharedSpaceResponse:
    """Create a new shared space.

    The creator is automatically added as an OWNER.

    Requires VIEWER role or higher (any authenticated user).
    """
    require_role(current_user.role, UserRole.VIEWER)

    service = SharedSpaceService(db)

    try:
        space = await service.create_space(
            tenant_id=current_user.tenant_id,
            name=request.name,
            creator_id=current_user.user_id,
            description=request.description,
        )

        log.info(
            "api.space_created",
            space_id=str(space.id),
            user_id=str(current_user.user_id),
            tenant_id=str(current_user.tenant_id),
        )

        return SharedSpaceResponse.from_space(space)

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.get("", response_model=list[SharedSpaceResponse])
async def list_spaces(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[SharedSpaceResponse]:
    """List all spaces the current user is a member of.

    Returns only spaces within the user's tenant.

    Requires VIEWER role or higher.
    """
    require_role(current_user.role, UserRole.VIEWER)

    service = SharedSpaceService(db)

    spaces = await service.list_spaces(
        tenant_id=current_user.tenant_id,
        user_id=current_user.user_id,
    )

    log.info(
        "api.spaces_listed",
        count=len(spaces),
        user_id=str(current_user.user_id),
        tenant_id=str(current_user.tenant_id),
    )

    return [SharedSpaceResponse.from_space(space) for space in spaces]


@router.post("/{space_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_member(
    space_id: str,
    request: AddMemberRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Add a member to a space.

    Only space owners can add members.
    Member must belong to the same tenant as the space.

    Requires VIEWER role or higher.
    """
    require_role(current_user.role, UserRole.VIEWER)

    service = SharedSpaceService(db)

    try:
        space_uuid = uuid.UUID(space_id)
        user_uuid = uuid.UUID(request.user_id)

        # Check space exists and user has permission
        space = await service.get_space(space_uuid)
        if not space:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Space not found",
            )

        # Enforce tenant isolation
        if space.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Space not found",
            )

        await service.add_member(
            space_id=space_uuid,
            user_id=user_uuid,
            role=request.role,
            added_by=current_user.user_id,
        )

        log.info(
            "api.member_added",
            space_id=space_id,
            user_id=request.user_id,
            role=request.role,
            added_by=str(current_user.user_id),
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.delete("/{space_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    space_id: str,
    user_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Remove a member from a space.

    Only space owners can remove members.
    Cannot remove the last owner.

    Requires VIEWER role or higher.
    """
    require_role(current_user.role, UserRole.VIEWER)

    service = SharedSpaceService(db)

    try:
        space_uuid = uuid.UUID(space_id)
        user_uuid = uuid.UUID(user_id)

        # Check space exists and user has permission
        space = await service.get_space(space_uuid)
        if not space:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Space not found",
            )

        # Enforce tenant isolation
        if space.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Space not found",
            )

        await service.remove_member(
            space_id=space_uuid,
            user_id=user_uuid,
            removed_by=current_user.user_id,
        )

        log.info(
            "api.member_removed",
            space_id=space_id,
            user_id=user_id,
            removed_by=str(current_user.user_id),
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.post("/{space_id}/share", response_model=SharedConversationResponse, status_code=status.HTTP_201_CREATED)
async def share_conversation(
    space_id: str,
    request: ShareConversationRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> SharedConversationResponse:
    """Share a conversation to a space.

    User must be an OWNER or CONTRIBUTOR to share.
    Conversation must belong to the same tenant as the space.

    Requires VIEWER role or higher.
    """
    require_role(current_user.role, UserRole.VIEWER)

    service = SharedSpaceService(db)

    try:
        space_uuid = uuid.UUID(space_id)
        conversation_uuid = uuid.UUID(request.conversation_id)

        # Check space exists and user has permission
        space = await service.get_space(space_uuid)
        if not space:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Space not found",
            )

        # Enforce tenant isolation
        if space.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Space not found",
            )

        shared = await service.share_conversation(
            space_id=space_uuid,
            conversation_id=conversation_uuid,
            user_id=current_user.user_id,
        )

        log.info(
            "api.conversation_shared",
            space_id=space_id,
            conversation_id=request.conversation_id,
            shared_by=str(current_user.user_id),
        )

        return SharedConversationResponse.from_shared_conversation(shared)

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.get("/{space_id}/conversations", response_model=list[SharedConversationResponse])
async def get_shared_conversations(
    space_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[SharedConversationResponse]:
    """Get all conversations shared to a space.

    User must be a member of the space.

    Requires VIEWER role or higher.
    """
    require_role(current_user.role, UserRole.VIEWER)

    service = SharedSpaceService(db)

    try:
        space_uuid = uuid.UUID(space_id)

        # Check space exists and user has permission
        space = await service.get_space(space_uuid)
        if not space:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Space not found",
            )

        # Enforce tenant isolation
        if space.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Space not found",
            )

        shared_conversations = await service.get_shared_conversations(
            space_id=space_uuid,
            user_id=current_user.user_id,
        )

        log.info(
            "api.shared_conversations_retrieved",
            space_id=space_id,
            count=len(shared_conversations),
            user_id=str(current_user.user_id),
        )

        return [
            SharedConversationResponse.from_shared_conversation(sc)
            for sc in shared_conversations
        ]

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
