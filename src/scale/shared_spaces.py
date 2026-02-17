"""Shared spaces for team collaboration.

Enables users within the same tenant to share conversations and collaborate.
All operations are strictly tenant-scoped - no cross-tenant sharing.

Permissions model:
- OWNER: Full control (add/remove members, share/unshare conversations, delete space)
- CONTRIBUTOR: Can share conversations and read shared content
- VIEWER: Read-only access to shared conversations

Key invariants:
- Spaces MUST belong to exactly one tenant
- Members MUST belong to the same tenant as the space
- Conversations MUST belong to the same tenant as the space
- No cross-tenant sharing under any circumstances
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.conversation import Conversation
from src.models.user import User

log = structlog.get_logger(__name__)


class SpaceRole(StrEnum):
    """Role within a shared space."""

    OWNER = "owner"  # Full control
    CONTRIBUTOR = "contributor"  # Share + read
    VIEWER = "viewer"  # Read-only


@dataclass
class SpaceMember:
    """Member of a shared space.

    Attributes:
        user_id: UUID of the user
        role: Permission level in the space
        added_at: When the member was added
        added_by: UUID of user who added this member
    """

    user_id: uuid.UUID
    role: SpaceRole
    added_at: datetime
    added_by: uuid.UUID


@dataclass
class SharedSpace:
    """A collaborative space within a tenant.

    Attributes:
        id: Unique space identifier
        tenant_id: Tenant this space belongs to
        name: Human-readable space name
        description: Optional space description
        created_by: UUID of user who created the space
        created_at: Creation timestamp
        members: List of space members with roles
        settings: Additional space configuration (JSONB)
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    created_by: uuid.UUID
    created_at: datetime
    members: list[SpaceMember] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)

    def has_member(self, user_id: uuid.UUID) -> bool:
        """Check if a user is a member of this space.

        Args:
            user_id: User UUID to check

        Returns:
            True if user is a member
        """
        return any(m.user_id == user_id for m in self.members)

    def get_member_role(self, user_id: uuid.UUID) -> SpaceRole | None:
        """Get the role of a user in this space.

        Args:
            user_id: User UUID

        Returns:
            SpaceRole if user is a member, None otherwise
        """
        for member in self.members:
            if member.user_id == user_id:
                return member.role
        return None

    def is_owner(self, user_id: uuid.UUID) -> bool:
        """Check if a user is an owner of this space.

        Args:
            user_id: User UUID

        Returns:
            True if user is an owner
        """
        return self.get_member_role(user_id) == SpaceRole.OWNER

    def can_manage_members(self, user_id: uuid.UUID) -> bool:
        """Check if a user can add/remove members.

        Args:
            user_id: User UUID

        Returns:
            True if user is an owner
        """
        return self.is_owner(user_id)

    def can_share_conversations(self, user_id: uuid.UUID) -> bool:
        """Check if a user can share conversations.

        Args:
            user_id: User UUID

        Returns:
            True if user is owner or contributor
        """
        role = self.get_member_role(user_id)
        return role in (SpaceRole.OWNER, SpaceRole.CONTRIBUTOR)

    def can_view(self, user_id: uuid.UUID) -> bool:
        """Check if a user can view shared content.

        Args:
            user_id: User UUID

        Returns:
            True if user is a member (any role)
        """
        return self.has_member(user_id)


@dataclass
class SharedConversation:
    """A conversation shared to a space.

    Attributes:
        conversation_id: UUID of the conversation
        space_id: UUID of the space
        shared_by: UUID of user who shared it
        shared_at: When it was shared
        permissions: Additional permissions (future: read-only, comment, etc.)
    """

    conversation_id: uuid.UUID
    space_id: uuid.UUID
    shared_by: uuid.UUID
    shared_at: datetime
    permissions: dict[str, Any] = field(default_factory=dict)


class SharedSpaceService:
    """Service for managing shared spaces and collaboration.

    All operations enforce tenant isolation - no cross-tenant access.

    Usage:
        service = SharedSpaceService(db_session)

        # Create space
        space = await service.create_space(
            tenant_id=tenant_id,
            name="Engineering Team",
            creator_id=user_id,
        )

        # Add member
        await service.add_member(
            space_id=space.id,
            user_id=member_id,
            role=SpaceRole.CONTRIBUTOR,
            added_by=user_id,
        )

        # Share conversation
        await service.share_conversation(
            space_id=space.id,
            conversation_id=conv_id,
            user_id=user_id,
        )

        # List shared conversations
        conversations = await service.get_shared_conversations(
            space_id=space.id,
            user_id=user_id,
        )
    """

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the service.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db
        # In-memory storage (non-persistent; migrate to DB for production multi-instance use)
        self._spaces: dict[uuid.UUID, SharedSpace] = {}
        self._shared_conversations: dict[uuid.UUID, list[SharedConversation]] = {}

    async def create_space(
        self,
        tenant_id: uuid.UUID,
        name: str,
        creator_id: uuid.UUID,
        description: str | None = None,
    ) -> SharedSpace:
        """Create a new shared space.

        The creator is automatically added as an OWNER.

        Args:
            tenant_id: Tenant UUID
            name: Space name
            creator_id: UUID of user creating the space
            description: Optional description

        Returns:
            Created SharedSpace

        Raises:
            ValueError: If creator is not in the tenant
        """
        # Verify creator belongs to tenant
        user = await self.db.get(User, creator_id)
        if not user or user.tenant_id != tenant_id:
            raise ValueError("Creator must belong to the specified tenant")

        space_id = uuid.uuid4()
        now = datetime.now(UTC)

        space = SharedSpace(
            id=space_id,
            tenant_id=tenant_id,
            name=name,
            description=description,
            created_by=creator_id,
            created_at=now,
            members=[
                SpaceMember(
                    user_id=creator_id,
                    role=SpaceRole.OWNER,
                    added_at=now,
                    added_by=creator_id,
                )
            ],
        )

        self._spaces[space_id] = space
        self._shared_conversations[space_id] = []

        log.info(
            "shared_space.created",
            space_id=str(space_id),
            tenant_id=str(tenant_id),
            creator_id=str(creator_id),
            name=name,
        )

        return space

    async def add_member(
        self,
        space_id: uuid.UUID,
        user_id: uuid.UUID,
        role: SpaceRole,
        added_by: uuid.UUID,
    ) -> None:
        """Add a member to a space.

        Args:
            space_id: Space UUID
            user_id: User UUID to add
            role: Role to assign
            added_by: UUID of user adding the member

        Raises:
            ValueError: If space not found, permission denied, user not in tenant, or user already member
        """
        space = self._spaces.get(space_id)
        if not space:
            raise ValueError("Space not found")

        # Check permission
        if not space.can_manage_members(added_by):
            raise ValueError("Only owners can add members")

        # Verify user exists and belongs to same tenant
        user = await self.db.get(User, user_id)
        if not user or user.tenant_id != space.tenant_id:
            raise ValueError("User must belong to the same tenant as the space")

        # Check if already a member
        if space.has_member(user_id):
            raise ValueError("User is already a member")

        # Add member
        member = SpaceMember(
            user_id=user_id,
            role=role,
            added_at=datetime.now(UTC),
            added_by=added_by,
        )
        space.members.append(member)

        log.info(
            "shared_space.member_added",
            space_id=str(space_id),
            user_id=str(user_id),
            role=role,
            added_by=str(added_by),
        )

    async def remove_member(
        self,
        space_id: uuid.UUID,
        user_id: uuid.UUID,
        removed_by: uuid.UUID,
    ) -> None:
        """Remove a member from a space.

        Args:
            space_id: Space UUID
            user_id: User UUID to remove
            removed_by: UUID of user removing the member

        Raises:
            ValueError: If space not found, permission denied, or user not a member
        """
        space = self._spaces.get(space_id)
        if not space:
            raise ValueError("Space not found")

        # Check permission
        if not space.can_manage_members(removed_by):
            raise ValueError("Only owners can remove members")

        # Cannot remove the creator (last owner protection)
        if user_id == space.created_by:
            owner_count = sum(1 for m in space.members if m.role == SpaceRole.OWNER)
            if owner_count <= 1:
                raise ValueError("Cannot remove the last owner")

        # Remove member
        original_count = len(space.members)
        space.members = [m for m in space.members if m.user_id != user_id]

        if len(space.members) == original_count:
            raise ValueError("User is not a member")

        log.info(
            "shared_space.member_removed",
            space_id=str(space_id),
            user_id=str(user_id),
            removed_by=str(removed_by),
        )

    async def share_conversation(
        self,
        space_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> SharedConversation:
        """Share a conversation to a space.

        Args:
            space_id: Space UUID
            conversation_id: Conversation UUID to share
            user_id: UUID of user sharing the conversation

        Returns:
            SharedConversation record

        Raises:
            ValueError: If space not found, permission denied, conversation not found, or tenant mismatch
        """
        space = self._spaces.get(space_id)
        if not space:
            raise ValueError("Space not found")

        # Check permission
        if not space.can_share_conversations(user_id):
            raise ValueError("User does not have permission to share conversations")

        # Verify conversation exists and belongs to same tenant
        conversation = await self.db.get(Conversation, conversation_id)
        if not conversation:
            raise ValueError("Conversation not found")

        if conversation.tenant_id != space.tenant_id:
            raise ValueError("Conversation must belong to the same tenant as the space")

        # Check if already shared
        existing = self._shared_conversations.get(space_id, [])
        if any(sc.conversation_id == conversation_id for sc in existing):
            raise ValueError("Conversation already shared to this space")

        # Share conversation
        shared = SharedConversation(
            conversation_id=conversation_id,
            space_id=space_id,
            shared_by=user_id,
            shared_at=datetime.now(UTC),
        )

        if space_id not in self._shared_conversations:
            self._shared_conversations[space_id] = []

        self._shared_conversations[space_id].append(shared)

        log.info(
            "shared_space.conversation_shared",
            space_id=str(space_id),
            conversation_id=str(conversation_id),
            shared_by=str(user_id),
        )

        return shared

    async def get_shared_conversations(
        self,
        space_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[SharedConversation]:
        """Get all conversations shared to a space.

        Args:
            space_id: Space UUID
            user_id: UUID of user requesting the list

        Returns:
            List of SharedConversation records

        Raises:
            ValueError: If space not found or user not a member
        """
        space = self._spaces.get(space_id)
        if not space:
            raise ValueError("Space not found")

        # Check permission
        if not space.can_view(user_id):
            raise ValueError("User is not a member of this space")

        return self._shared_conversations.get(space_id, [])

    async def list_spaces(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[SharedSpace]:
        """List all spaces a user is a member of.

        Args:
            tenant_id: Tenant UUID
            user_id: User UUID

        Returns:
            List of SharedSpace objects the user is a member of
        """
        return [
            space
            for space in self._spaces.values()
            if space.tenant_id == tenant_id and space.has_member(user_id)
        ]

    async def get_space(self, space_id: uuid.UUID) -> SharedSpace | None:
        """Get a space by ID.

        Args:
            space_id: Space UUID

        Returns:
            SharedSpace if found, None otherwise
        """
        return self._spaces.get(space_id)
