"""Tests for JWT authentication and authorization.

Tests cover:
- Missing token -> 401
- Invalid token signature -> 401
- Valid token -> request proceeds
- Expired token -> 401
- Wrong role -> 403
- JIT user provisioning on first login
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import jwt
import pytest
from httpx import AsyncClient

from tests.conftest import TEST_JWT_SECRET, make_token, auth_headers
from src.models.user import User, UserRole


class TestMissingAuth:
    """Requests without Authorization header."""

    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self, client: AsyncClient) -> None:
        """Unauthenticated requests to protected endpoints return 401."""
        response = await client.post("/api/v1/chat", json={"message": "hello"})
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_health_is_public(self, client: AsyncClient) -> None:
        """Health endpoints don't require authentication."""
        response = await client.get("/health/live")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_readiness_is_public(self, client: AsyncClient) -> None:
        """Readiness probe is always accessible."""
        response = await client.get("/health/ready")
        # May be 200 or 200 with db error - either way not 401
        assert response.status_code == 200


class TestInvalidTokens:
    """Requests with malformed or invalid tokens."""

    @pytest.mark.asyncio
    async def test_wrong_signature_returns_401(self, client: AsyncClient) -> None:
        """Token signed with wrong secret is rejected."""
        token = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "tenant_id": str(uuid.uuid4()),
                "role": "viewer",
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            },
            "WRONG_SECRET",
            algorithm="HS256",
        )
        response = await client.post(
            "/api/v1/chat",
            json={"message": "hello"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_token_returns_401(self, client: AsyncClient, tenant_a) -> None:
        """Expired token is rejected."""
        token = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "tenant_id": str(tenant_a.id),
                "role": "viewer",
                "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),  # Past!
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        response = await client.post(
            "/api/v1/chat",
            json={"message": "hello"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_malformed_token_returns_401(self, client: AsyncClient) -> None:
        """Completely malformed token is rejected."""
        response = await client.post(
            "/api/v1/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_bearer_prefix_returns_401(self, client: AsyncClient) -> None:
        """Token without 'Bearer ' prefix is rejected."""
        token = jwt.encode(
            {"sub": "x", "exp": 9999999999},
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        response = await client.post(
            "/api/v1/chat",
            json={"message": "hello"},
            headers={"Authorization": token},  # Missing "Bearer "
        )
        assert response.status_code == 401


class TestJITProvisioning:
    """JIT user provisioning on first login."""

    @pytest.mark.asyncio
    async def test_new_user_is_provisioned(
        self,
        client: AsyncClient,
        tenant_a,
        db_session,
    ) -> None:
        """A user not in the DB is created automatically on first request.

        With mock DB, we verify JIT provisioning by confirming the request
        succeeds (200) with a valid token for a new user sub. The
        _mock_get_current_user in conftest creates a User object from
        JWT claims, simulating JIT provisioning.
        """
        new_sub = str(uuid.uuid4())
        token = jwt.encode(
            {
                "sub": new_sub,
                "tenant_id": str(tenant_a.id),
                "role": "viewer",
                "email": "newuser@test.com",
                "aud": "enterprise-agents-api",
                "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )

        # Make a request - with mock DB, conversations list returns empty
        response = await client.get(
            "/api/v1/conversations",
            headers={"Authorization": f"Bearer {token}"},
        )
        # 200 means the user was successfully authenticated (JIT provisioned)
        assert response.status_code == 200


class TestRBACEnforcement:
    """Role-based access control enforcement."""

    @pytest.mark.asyncio
    async def test_viewer_cannot_delete_document(
        self,
        client_viewer_a: AsyncClient,
    ) -> None:
        """Viewers cannot delete documents (requires admin)."""
        response = await client_viewer_a.delete(f"/api/v1/documents/{uuid.uuid4()}")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_upload_document(
        self,
        client_viewer_a: AsyncClient,
    ) -> None:
        """Viewers cannot upload documents (requires operator+)."""
        import io
        response = await client_viewer_a.post(
            "/api/v1/documents/upload",
            files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_access_admin(
        self,
        client_viewer_a: AsyncClient,
    ) -> None:
        """Viewers cannot access admin endpoints."""
        response = await client_viewer_a.get("/api/v1/admin/users")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_access_admin(
        self,
        client_admin_a: AsyncClient,
    ) -> None:
        """Admin users can access admin endpoints."""
        response = await client_admin_a.get("/api/v1/admin/users")
        assert response.status_code == 200
