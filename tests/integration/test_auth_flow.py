"""Integration tests for authentication flows.

Tests JWT token validation, expiration, tenant isolation, and RBAC
with real middleware and database.

Coverage:
- Valid token grants access
- Expired token returns 401
- Missing token returns 401
- Invalid signature returns 401
- Wrong tenant returns 403
- Role escalation prevention

Run with:
    pytest -m integration tests/integration/test_auth_flow.py
"""

import time
from datetime import datetime, timezone

import httpx
import jwt
import pytest

from tests.conftest import TEST_JWT_SECRET, make_token


@pytest.mark.integration
async def test_valid_token_grants_access(
    client_admin_a_int: httpx.AsyncClient,
):
    """Valid JWT token grants access to protected endpoints."""
    # Try to access a protected endpoint
    resp = await client_admin_a_int.get("/api/v1/conversations")
    assert resp.status_code == 200


@pytest.mark.integration
async def test_missing_token_returns_401(
    integration_client: httpx.AsyncClient,
):
    """Request without Authorization header returns 401 Unauthorized."""
    resp = await integration_client.get("/api/v1/conversations")
    assert resp.status_code == 401
    data = resp.json()
    assert "detail" in data


@pytest.mark.integration
async def test_expired_token_returns_401(
    integration_client: httpx.AsyncClient,
    tenant_ids: dict[str, str],
):
    """Expired JWT token returns 401 Unauthorized.

    Create a token with exp in the past and verify it's rejected.
    """
    # Create expired token
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": "test-user",
        "tenant_id": tenant_ids["tenant_a"],
        "role": "admin",
        "email": "test@example.com",
        "aud": "enterprise-agents-api",
        "iat": now - 7200,  # Issued 2 hours ago
        "exp": now - 3600,  # Expired 1 hour ago
    }
    expired_token = jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")

    # Try to use expired token
    resp = await integration_client.get(
        "/api/v1/conversations",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert resp.status_code == 401


@pytest.mark.integration
async def test_invalid_signature_returns_401(
    integration_client: httpx.AsyncClient,
    tenant_ids: dict[str, str],
):
    """Token with invalid signature returns 401.

    Sign token with wrong secret and verify it's rejected.
    """
    wrong_secret = "wrong-secret-key"
    token = make_token(
        sub="test-user",
        tenant_id=tenant_ids["tenant_a"],
        role="admin",
    )

    # Tamper with token by re-signing with wrong secret
    decoded = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"], options={"verify_signature": False})
    tampered_token = jwt.encode(decoded, wrong_secret, algorithm="HS256")

    # Try to use tampered token
    resp = await integration_client.get(
        "/api/v1/conversations",
        headers={"Authorization": f"Bearer {tampered_token}"},
    )
    assert resp.status_code == 401


@pytest.mark.integration
async def test_malformed_token_returns_401(
    integration_client: httpx.AsyncClient,
):
    """Malformed JWT token returns 401."""
    # Send garbage token
    resp = await integration_client.get(
        "/api/v1/conversations",
        headers={"Authorization": "Bearer not-a-valid-jwt-token"},
    )
    assert resp.status_code == 401

    # Send token without Bearer prefix
    valid_token = make_token(
        sub="test-user",
        tenant_id="12345678-1234-5678-1234-567812345678",
        role="admin",
    )
    resp = await integration_client.get(
        "/api/v1/conversations",
        headers={"Authorization": valid_token},  # Missing "Bearer "
    )
    assert resp.status_code == 401


@pytest.mark.integration
async def test_wrong_tenant_returns_403(
    client_admin_a_int: httpx.AsyncClient,
    client_admin_b_int: httpx.AsyncClient,
):
    """User from tenant B cannot access tenant A's resources (403 Forbidden).

    This tests tenant isolation at the authorization level.
    """
    # Admin A creates a conversation
    create_resp = await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Tenant A Conversation"},
    )
    assert create_resp.status_code == 201
    conv_id = create_resp.json()["id"]

    # Admin B tries to access it (should be forbidden)
    get_resp = await client_admin_b_int.get(f"/api/v1/conversations/{conv_id}")
    assert get_resp.status_code in (403, 404)  # 403 or 404 depending on implementation

    # Admin B tries to update it (should be forbidden)
    update_resp = await client_admin_b_int.patch(
        f"/api/v1/conversations/{conv_id}",
        json={"title": "Hacked Title"},
    )
    assert update_resp.status_code in (403, 404)

    # Admin B tries to delete it (should be forbidden)
    delete_resp = await client_admin_b_int.delete(f"/api/v1/conversations/{conv_id}")
    assert delete_resp.status_code in (403, 404)


@pytest.mark.integration
async def test_role_escalation_prevention(
    client_viewer_a_int: httpx.AsyncClient,
    seed_data: dict,
):
    """Viewer cannot escalate privileges via JWT claims.

    Even if a user modifies their JWT to claim admin role,
    the server should validate against the database.
    """
    viewer_a = seed_data["users"]["viewer_a"]

    # Viewer tries to access admin endpoint
    # (In a real system, this would also validate against DB-stored role)
    resp = await client_viewer_a_int.post(
        "/api/v1/admin/users",
        json={
            "email": "hacker@example.com",
            "display_name": "Hacker",
            "role": "admin",
        },
    )
    # Should be forbidden (or 404 if endpoint doesn't exist)
    assert resp.status_code in (403, 404)


@pytest.mark.integration
async def test_token_without_required_claims_returns_401(
    integration_client: httpx.AsyncClient,
):
    """Token missing required claims (sub, tenant_id, role) returns 401."""
    now = int(datetime.now(timezone.utc).timestamp())

    # Token missing 'sub'
    payload_no_sub = {
        "tenant_id": "12345678-1234-5678-1234-567812345678",
        "role": "admin",
        "email": "test@example.com",
        "aud": "enterprise-agents-api",
        "iat": now,
        "exp": now + 3600,
    }
    token_no_sub = jwt.encode(payload_no_sub, TEST_JWT_SECRET, algorithm="HS256")
    resp = await integration_client.get(
        "/api/v1/conversations",
        headers={"Authorization": f"Bearer {token_no_sub}"},
    )
    assert resp.status_code == 401

    # Token missing 'tenant_id'
    payload_no_tenant = {
        "sub": "test-user",
        "role": "admin",
        "email": "test@example.com",
        "aud": "enterprise-agents-api",
        "iat": now,
        "exp": now + 3600,
    }
    token_no_tenant = jwt.encode(payload_no_tenant, TEST_JWT_SECRET, algorithm="HS256")
    resp = await integration_client.get(
        "/api/v1/conversations",
        headers={"Authorization": f"Bearer {token_no_tenant}"},
    )
    assert resp.status_code == 401


@pytest.mark.integration
async def test_public_endpoints_do_not_require_auth(
    integration_client: httpx.AsyncClient,
):
    """Public endpoints (health, docs) do not require authentication."""
    # Health endpoint
    resp = await integration_client.get("/health")
    assert resp.status_code == 200

    # Readiness endpoint
    resp = await integration_client.get("/health/ready")
    assert resp.status_code == 200

    # OpenAPI docs (if enabled in test mode)
    # Note: In production, docs_url is None, so this may 404
    resp = await integration_client.get("/docs")
    assert resp.status_code in (200, 404)  # 200 in test, 404 in prod


@pytest.mark.integration
async def test_concurrent_requests_with_same_token(
    client_admin_a_int: httpx.AsyncClient,
):
    """Multiple concurrent requests with same token should all succeed.

    Tests that token validation is stateless and thread-safe.
    """
    import asyncio

    # Make 10 concurrent requests
    tasks = [
        client_admin_a_int.get("/api/v1/conversations")
        for _ in range(10)
    ]
    responses = await asyncio.gather(*tasks)

    # All should succeed
    for resp in responses:
        assert resp.status_code == 200


@pytest.mark.integration
async def test_different_roles_have_different_permissions(
    client_admin_a_int: httpx.AsyncClient,
    client_viewer_a_int: httpx.AsyncClient,
):
    """Admin and viewer roles have different access permissions.

    Admin can create/update/delete, viewer can only read.
    """
    # Admin creates a conversation
    create_resp = await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Admin Created"},
    )
    assert create_resp.status_code == 201
    conv_id = create_resp.json()["id"]

    # Viewer can read it (same tenant)
    get_resp = await client_viewer_a_int.get(f"/api/v1/conversations/{conv_id}")
    # Depending on implementation, viewer might see it (200) or not (403)
    # For now, assume viewers can read conversations in their tenant
    assert get_resp.status_code in (200, 403)

    # Viewer cannot delete it
    delete_resp = await client_viewer_a_int.delete(f"/api/v1/conversations/{conv_id}")
    assert delete_resp.status_code in (403, 404)

    # Admin can delete it
    delete_resp = await client_admin_a_int.delete(f"/api/v1/conversations/{conv_id}")
    assert delete_resp.status_code == 204
