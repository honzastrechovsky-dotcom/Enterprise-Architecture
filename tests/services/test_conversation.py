"""Tests for ConversationService."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from src.models.conversation import Conversation, Message, MessageRole
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.services.conversation import ConversationService

# All tests require a real database (add, commit, query).
pytestmark = pytest.mark.integration


@pytest.fixture
async def tenant(db_session):
    """Create a test tenant."""
    tenant = Tenant(
        name="Test Tenant",
        slug="test-tenant",
        is_active=True,
    )
    db_session.add(tenant)
    await db_session.flush()
    return tenant


@pytest.fixture
async def user(db_session, tenant):
    """Create a test user."""
    user = User(
        tenant_id=tenant.id,
        external_id="test-user-external-id",
        email="test@example.com",
        display_name="Test User",
        role=UserRole.VIEWER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest.fixture
def service(db_session):
    """Create ConversationService instance."""
    return ConversationService(db_session)


class TestCreateConversation:
    """Test conversation creation."""

    @pytest.mark.asyncio
    async def test_create_conversation_with_title(self, service, tenant, user, db_session):
        """Test creating a conversation with a title."""
        conversation = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Test Conversation",
        )

        assert conversation.id is not None
        assert conversation.tenant_id == tenant.id
        assert conversation.user_id == user.id
        assert conversation.title == "Test Conversation"
        assert conversation.is_archived is False
        assert conversation.metadata_ == {}

    @pytest.mark.asyncio
    async def test_create_conversation_with_metadata(self, service, tenant, user, db_session):
        """Test creating a conversation with metadata."""
        metadata = {"model": "gpt-4", "temperature": 0.7}
        conversation = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Metadata Test",
            metadata=metadata,
        )

        assert conversation.metadata_ == metadata

    @pytest.mark.asyncio
    async def test_create_conversation_without_title(self, service, tenant, user):
        """Test creating a conversation without a title."""
        conversation = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )

        assert conversation.title is None


class TestListConversations:
    """Test listing conversations."""

    @pytest.mark.asyncio
    async def test_list_conversations_for_user(self, service, tenant, user, db_session):
        """Test listing conversations for a specific user."""
        # Create conversations
        conv1 = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="First",
        )
        conv2 = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Second",
        )

        conversations = await service.list_conversations(
            tenant_id=tenant.id,
            user_id=user.id,
        )

        assert len(conversations) == 2
        # Most recent first
        assert conversations[0].id == conv2.id
        assert conversations[1].id == conv1.id

    @pytest.mark.asyncio
    async def test_list_conversations_excludes_archived_by_default(
        self, service, tenant, user, db_session
    ):
        """Test that archived conversations are excluded by default."""
        conv1 = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Active",
        )
        conv2 = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Archived",
        )
        await service.archive_conversation(
            conversation_id=conv2.id,
            tenant_id=tenant.id,
            archived=True,
        )

        conversations = await service.list_conversations(
            tenant_id=tenant.id,
            user_id=user.id,
            include_archived=False,
        )

        assert len(conversations) == 1
        assert conversations[0].id == conv1.id

    @pytest.mark.asyncio
    async def test_list_conversations_includes_archived_when_requested(
        self, service, tenant, user, db_session
    ):
        """Test that archived conversations are included when requested."""
        await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Active",
        )
        conv2 = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Archived",
        )
        await service.archive_conversation(
            conversation_id=conv2.id,
            tenant_id=tenant.id,
            archived=True,
        )

        conversations = await service.list_conversations(
            tenant_id=tenant.id,
            user_id=user.id,
            include_archived=True,
        )

        assert len(conversations) == 2

    @pytest.mark.asyncio
    async def test_list_conversations_respects_limit(self, service, tenant, user):
        """Test that limit parameter works."""
        for i in range(5):
            await service.create_conversation(
                tenant_id=tenant.id,
                user_id=user.id,
                title=f"Conversation {i}",
            )

        conversations = await service.list_conversations(
            tenant_id=tenant.id,
            user_id=user.id,
            limit=3,
        )

        assert len(conversations) == 3

    @pytest.mark.asyncio
    async def test_list_conversations_respects_offset(self, service, tenant, user):
        """Test that offset parameter works."""
        for i in range(5):
            await service.create_conversation(
                tenant_id=tenant.id,
                user_id=user.id,
                title=f"Conversation {i}",
            )

        conversations = await service.list_conversations(
            tenant_id=tenant.id,
            user_id=user.id,
            limit=10,
            offset=2,
        )

        assert len(conversations) == 3


class TestGetConversation:
    """Test getting a conversation by ID."""

    @pytest.mark.asyncio
    async def test_get_conversation_returns_conversation(self, service, tenant, user):
        """Test getting an existing conversation."""
        created = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Test",
        )

        found = await service.get_conversation(
            conversation_id=created.id,
            tenant_id=tenant.id,
        )

        assert found is not None
        assert found.id == created.id

    @pytest.mark.asyncio
    async def test_get_conversation_returns_none_for_wrong_tenant(
        self, service, tenant, user, db_session
    ):
        """Test that conversation cannot be accessed from different tenant."""
        created = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Test",
        )

        # Try to access from different tenant
        wrong_tenant_id = uuid.uuid4()
        found = await service.get_conversation(
            conversation_id=created.id,
            tenant_id=wrong_tenant_id,
        )

        assert found is None

    @pytest.mark.asyncio
    async def test_get_conversation_with_messages(self, service, tenant, user, db_session):
        """Test getting conversation with messages loaded."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Test",
        )

        # Add messages
        await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.USER,
            content="Hello",
        )
        await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.ASSISTANT,
            content="Hi there",
        )

        found = await service.get_conversation(
            conversation_id=conv.id,
            tenant_id=tenant.id,
            include_messages=True,
        )

        assert found is not None
        assert len(found.messages) == 2


class TestAddMessage:
    """Test adding messages to conversations."""

    @pytest.mark.asyncio
    async def test_add_message_to_conversation(self, service, tenant, user, db_session):
        """Test adding a message to a conversation."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )

        message = await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.USER,
            content="Test message",
        )

        assert message.id is not None
        assert message.conversation_id == conv.id
        assert message.role == MessageRole.USER
        assert message.content == "Test message"
        assert message.sequence_number == 1

    @pytest.mark.asyncio
    async def test_add_message_increments_sequence(self, service, tenant, user):
        """Test that sequence numbers increment correctly."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )

        msg1 = await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.USER,
            content="First",
        )
        msg2 = await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.ASSISTANT,
            content="Second",
        )
        msg3 = await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.USER,
            content="Third",
        )

        assert msg1.sequence_number == 1
        assert msg2.sequence_number == 2
        assert msg3.sequence_number == 3

    @pytest.mark.asyncio
    async def test_add_message_with_metadata(self, service, tenant, user):
        """Test adding a message with optional metadata."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )

        message = await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.ASSISTANT,
            content="Response",
            model_used="gpt-4",
            token_count=150,
            citations=[{"doc_id": "123", "content": "citation"}],
            tool_calls=[{"tool": "calculator", "args": {"x": 5}}],
        )

        assert message.model_used == "gpt-4"
        assert message.token_count == 150
        assert len(message.citations) == 1
        assert len(message.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_add_message_raises_for_nonexistent_conversation(self, service, tenant):
        """Test that adding to nonexistent conversation raises error."""
        fake_conv_id = uuid.uuid4()

        with pytest.raises(ValueError, match="not found"):
            await service.add_message(
                conversation_id=fake_conv_id,
                role=MessageRole.USER,
                content="Test",
            )


class TestArchiveConversation:
    """Test archiving conversations."""

    @pytest.mark.asyncio
    async def test_archive_conversation(self, service, tenant, user, db_session):
        """Test archiving a conversation."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )

        result = await service.archive_conversation(
            conversation_id=conv.id,
            tenant_id=tenant.id,
            archived=True,
        )

        assert result is not None
        assert result.is_archived is True

    @pytest.mark.asyncio
    async def test_unarchive_conversation(self, service, tenant, user):
        """Test unarchiving a conversation."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )
        await service.archive_conversation(
            conversation_id=conv.id,
            tenant_id=tenant.id,
            archived=True,
        )

        result = await service.archive_conversation(
            conversation_id=conv.id,
            tenant_id=tenant.id,
            archived=False,
        )

        assert result is not None
        assert result.is_archived is False


class TestDeleteConversation:
    """Test deleting conversations."""

    @pytest.mark.asyncio
    async def test_delete_conversation(self, service, tenant, user, db_session):
        """Test deleting a conversation."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )

        deleted = await service.delete_conversation(
            conversation_id=conv.id,
            tenant_id=tenant.id,
        )

        assert deleted is True

        # Verify it's gone
        found = await service.get_conversation(
            conversation_id=conv.id,
            tenant_id=tenant.id,
        )
        assert found is None

    @pytest.mark.asyncio
    async def test_delete_conversation_cascades_to_messages(
        self, service, tenant, user, db_session
    ):
        """Test that deleting conversation also deletes messages."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )
        msg = await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.USER,
            content="Test",
        )

        await service.delete_conversation(
            conversation_id=conv.id,
            tenant_id=tenant.id,
        )

        # Verify message is gone
        stmt = select(Message).where(Message.id == msg.id)
        result = await db_session.execute(stmt)
        found = result.scalar_one_or_none()
        assert found is None


class TestSearchConversations:
    """Test searching conversations."""

    @pytest.mark.asyncio
    async def test_search_finds_matching_conversations(self, service, tenant, user):
        """Test searching for conversations by message content."""
        conv1 = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Python Discussion",
        )
        await service.add_message(
            conversation_id=conv1.id,
            role=MessageRole.USER,
            content="Tell me about Python programming",
        )

        conv2 = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="JavaScript Discussion",
        )
        await service.add_message(
            conversation_id=conv2.id,
            role=MessageRole.USER,
            content="Tell me about JavaScript",
        )

        results = await service.search_conversations(
            tenant_id=tenant.id,
            user_id=user.id,
            query="Python",
        )

        assert len(results) == 1
        assert results[0].id == conv1.id

    @pytest.mark.asyncio
    async def test_search_is_case_insensitive(self, service, tenant, user):
        """Test that search is case insensitive."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )
        await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.USER,
            content="This is a TEST message",
        )

        results = await service.search_conversations(
            tenant_id=tenant.id,
            user_id=user.id,
            query="test",
        )

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_returns_empty_for_no_matches(self, service, tenant, user):
        """Test that search returns empty list when no matches."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
        )
        await service.add_message(
            conversation_id=conv.id,
            role=MessageRole.USER,
            content="Python programming",
        )

        results = await service.search_conversations(
            tenant_id=tenant.id,
            user_id=user.id,
            query="Rust",
        )

        assert len(results) == 0


class TestUpdateConversationTitle:
    """Test updating conversation title."""

    @pytest.mark.asyncio
    async def test_update_conversation_title(self, service, tenant, user):
        """Test updating a conversation's title."""
        conv = await service.create_conversation(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Original Title",
        )

        updated = await service.update_conversation_title(
            conversation_id=conv.id,
            tenant_id=tenant.id,
            title="New Title",
        )

        assert updated is not None
        assert updated.title == "New Title"
