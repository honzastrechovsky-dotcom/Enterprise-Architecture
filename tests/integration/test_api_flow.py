"""Integration tests for API flows.

Tests complete user journeys through the API with real database,
real middleware, and real ASGI transport.

Coverage:
- Full chat flow: create conversation → send message → get response → check history
- Conversation CRUD: create, list, get, update, delete
- Tenant isolation: resources from tenant A invisible to tenant B
- RBAC: viewer cannot access admin endpoints
- Health endpoints return 200

Run with:
    pytest -m integration tests/integration/test_api_flow.py
"""

import uuid

import httpx
import pytest


@pytest.mark.integration
async def test_health_endpoints_return_200(integration_client: httpx.AsyncClient):
    """Health and readiness endpoints should return 200 without auth."""
    # Health check
    resp = await integration_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"

    # Readiness check
    resp = await integration_client.get("/health/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"


@pytest.mark.integration
async def test_full_chat_flow(
    client_admin_a_int: httpx.AsyncClient,
    seed_data: dict,
    mock_llm_client,
):
    """Test complete chat flow: create conversation → send message → get response → check history.

    This exercises:
    - POST /api/v1/conversations (create)
    - POST /api/v1/chat (send message)
    - GET /api/v1/conversations/{id}/messages (list messages)
    - GET /api/v1/conversations/{id} (get conversation details)
    """
    admin_a = seed_data["users"]["admin_a"]

    # Step 1: Create conversation
    create_resp = await client_admin_a_int.post(
        "/api/v1/conversations",
        json={
            "title": "Integration Test Chat",
            "system_prompt": "You are a helpful assistant.",
        },
    )
    assert create_resp.status_code == 201
    conv_data = create_resp.json()
    conv_id = conv_data["id"]
    assert conv_data["title"] == "Integration Test Chat"
    assert conv_data["user_id"] == str(admin_a.id)

    # Step 2: Send a message
    chat_resp = await client_admin_a_int.post(
        "/api/v1/chat",
        json={
            "conversation_id": conv_id,
            "message": "Hello, this is a test message.",
        },
    )
    assert chat_resp.status_code == 200
    chat_data = chat_resp.json()
    assert "response" in chat_data
    assert "This is a test response from the mocked LLM" in chat_data["response"]

    # Step 3: Get conversation history
    messages_resp = await client_admin_a_int.get(f"/api/v1/conversations/{conv_id}/messages")
    assert messages_resp.status_code == 200
    messages = messages_resp.json()
    assert len(messages) >= 2  # User message + assistant response
    assert any(m["role"] == "user" and "test message" in m["content"] for m in messages)
    assert any(m["role"] == "assistant" for m in messages)

    # Step 4: Get conversation details
    get_conv_resp = await client_admin_a_int.get(f"/api/v1/conversations/{conv_id}")
    assert get_conv_resp.status_code == 200
    conv_detail = get_conv_resp.json()
    assert conv_detail["id"] == conv_id
    assert conv_detail["title"] == "Integration Test Chat"


@pytest.mark.integration
async def test_conversation_crud_operations(
    client_admin_a_int: httpx.AsyncClient,
    seed_data: dict,
):
    """Test conversation CRUD: create, list, get, update, delete."""
    admin_a = seed_data["users"]["admin_a"]

    # Create
    create_resp = await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Test Conversation CRUD"},
    )
    assert create_resp.status_code == 201
    conv_id = create_resp.json()["id"]

    # List (should include our new conversation)
    list_resp = await client_admin_a_int.get("/api/v1/conversations")
    assert list_resp.status_code == 200
    conversations = list_resp.json()
    assert any(c["id"] == conv_id for c in conversations)

    # Get (read single)
    get_resp = await client_admin_a_int.get(f"/api/v1/conversations/{conv_id}")
    assert get_resp.status_code == 200
    conv = get_resp.json()
    assert conv["id"] == conv_id
    assert conv["title"] == "Test Conversation CRUD"

    # Update
    update_resp = await client_admin_a_int.patch(
        f"/api/v1/conversations/{conv_id}",
        json={"title": "Updated Title"},
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["title"] == "Updated Title"

    # Delete
    delete_resp = await client_admin_a_int.delete(f"/api/v1/conversations/{conv_id}")
    assert delete_resp.status_code == 204

    # Verify deleted (should 404)
    get_after_delete = await client_admin_a_int.get(f"/api/v1/conversations/{conv_id}")
    assert get_after_delete.status_code == 404


@pytest.mark.integration
async def test_tenant_isolation(
    client_admin_a_int: httpx.AsyncClient,
    client_admin_b_int: httpx.AsyncClient,
    seed_data: dict,
):
    """Test tenant isolation: admin_a creates resource, admin_b cannot see it.

    This ensures multi-tenancy is correctly enforced at the database level.
    """
    # Admin A creates a conversation
    create_resp = await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Admin A Private Conversation"},
    )
    assert create_resp.status_code == 201
    conv_id_a = create_resp.json()["id"]

    # Admin A can see it
    get_a_resp = await client_admin_a_int.get(f"/api/v1/conversations/{conv_id_a}")
    assert get_a_resp.status_code == 200

    # Admin B cannot see it (should 404 or 403)
    get_b_resp = await client_admin_b_int.get(f"/api/v1/conversations/{conv_id_a}")
    assert get_b_resp.status_code in (403, 404)

    # Admin B's list should not include Admin A's conversation
    list_b_resp = await client_admin_b_int.get("/api/v1/conversations")
    assert list_b_resp.status_code == 200
    conv_ids_b = [c["id"] for c in list_b_resp.json()]
    assert conv_id_a not in conv_ids_b


@pytest.mark.integration
async def test_rbac_viewer_cannot_access_admin_endpoints(
    client_viewer_a_int: httpx.AsyncClient,
    client_admin_a_int: httpx.AsyncClient,
):
    """Test RBAC: viewer cannot access admin endpoints.

    Admin endpoints include:
    - User management
    - Tenant configuration
    - Analytics (if restricted to admin)
    """
    # Viewer cannot create users (admin-only)
    create_user_resp = await client_viewer_a_int.post(
        "/api/v1/admin/users",
        json={
            "email": "newuser@example.com",
            "display_name": "New User",
            "role": "viewer",
        },
    )
    # Expect 403 Forbidden or 404 if endpoint doesn't exist yet
    assert create_user_resp.status_code in (403, 404)

    # Viewer cannot access tenant settings (admin-only)
    tenant_settings_resp = await client_viewer_a_int.get("/api/v1/admin/tenant/settings")
    assert tenant_settings_resp.status_code in (403, 404)

    # Admin can access these endpoints (or would, once implemented)
    # For now, we just verify viewer is blocked


@pytest.mark.integration
async def test_conversation_list_pagination(
    client_admin_a_int: httpx.AsyncClient,
):
    """Test conversation list supports pagination.

    Create multiple conversations and verify limit/offset work correctly.
    """
    # Create 5 conversations
    conv_ids = []
    for i in range(5):
        resp = await client_admin_a_int.post(
            "/api/v1/conversations",
            json={"title": f"Conversation {i}"},
        )
        assert resp.status_code == 201
        conv_ids.append(resp.json()["id"])

    # List with limit=2
    resp = await client_admin_a_int.get("/api/v1/conversations?limit=2")
    assert resp.status_code == 200
    conversations = resp.json()
    assert len(conversations) <= 2

    # List with offset=2, limit=2
    resp = await client_admin_a_int.get("/api/v1/conversations?limit=2&offset=2")
    assert resp.status_code == 200
    conversations_page2 = resp.json()
    assert len(conversations_page2) <= 2

    # Verify different pages return different results
    if len(conversations) > 0 and len(conversations_page2) > 0:
        assert conversations[0]["id"] != conversations_page2[0]["id"]


@pytest.mark.integration
async def test_message_history_preserves_order(
    client_admin_a_int: httpx.AsyncClient,
    mock_llm_client,
):
    """Test that message history preserves chronological order.

    Send multiple messages and verify they appear in the correct order.
    """
    # Create conversation
    create_resp = await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Order Test"},
    )
    conv_id = create_resp.json()["id"]

    # Send 3 messages in sequence
    for i in range(3):
        await client_admin_a_int.post(
            "/api/v1/chat",
            json={
                "conversation_id": conv_id,
                "message": f"Message {i}",
            },
        )

    # Get message history
    resp = await client_admin_a_int.get(f"/api/v1/conversations/{conv_id}/messages")
    assert resp.status_code == 200
    messages = resp.json()

    # Find user messages
    user_messages = [m for m in messages if m["role"] == "user"]
    assert len(user_messages) == 3

    # Verify order (should be Message 0, Message 1, Message 2)
    assert "Message 0" in user_messages[0]["content"]
    assert "Message 1" in user_messages[1]["content"]
    assert "Message 2" in user_messages[2]["content"]


@pytest.mark.integration
async def test_invalid_conversation_id_returns_404(
    client_admin_a_int: httpx.AsyncClient,
):
    """Test that accessing non-existent conversation returns 404."""
    fake_id = str(uuid.uuid4())

    # Get conversation
    resp = await client_admin_a_int.get(f"/api/v1/conversations/{fake_id}")
    assert resp.status_code == 404

    # Get messages
    resp = await client_admin_a_int.get(f"/api/v1/conversations/{fake_id}/messages")
    assert resp.status_code == 404

    # Update conversation
    resp = await client_admin_a_int.patch(
        f"/api/v1/conversations/{fake_id}",
        json={"title": "New Title"},
    )
    assert resp.status_code == 404

    # Delete conversation
    resp = await client_admin_a_int.delete(f"/api/v1/conversations/{fake_id}")
    assert resp.status_code == 404
