"""Tests for authentication middleware.

Coverage:
- AuthMiddleware validates valid tokens and sets request.state.auth_claims
- AuthMiddleware skips public paths (/health, /docs, /openapi.json)
- AuthMiddleware handles missing Authorization header gracefully
- AuthMiddleware handles invalid tokens gracefully
- Request flow with and without middleware
"""

from __future__ import annotations

import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from src.auth.middleware import AuthMiddleware
from src.auth.oidc import create_dev_token
from src.config import Settings


TEST_SECRET = "test-secret-for-middleware"


@pytest.fixture
def mock_settings() -> Settings:
    """Mock settings for middleware tests."""
    return Settings(
        environment="dev",  # type: ignore[arg-type]
        dev_jwt_secret=TEST_SECRET,  # type: ignore[arg-type]
    )


async def protected_endpoint(request: Request) -> Response:
    """Test endpoint that returns auth claims from request.state."""
    claims = getattr(request.state, "auth_claims", None)
    if claims is None:
        return JSONResponse({"authenticated": False}, status_code=200)
    return JSONResponse({
        "authenticated": True,
        "sub": claims.get("sub"),
        "tenant_id": claims.get("tenant_id"),
    })


async def health_endpoint(request: Request) -> Response:
    """Public health endpoint."""
    return JSONResponse({"status": "healthy"})


@pytest_asyncio.fixture
async def app_with_middleware(mock_settings: Settings) -> AsyncGenerator[Starlette, None]:
    """Test app with AuthMiddleware installed."""
    routes = [
        Route("/api/test", protected_endpoint, methods=["GET"]),
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/docs", health_endpoint, methods=["GET"]),
        Route("/openapi.json", health_endpoint, methods=["GET"]),
    ]

    app = Starlette(routes=routes)
    app.add_middleware(AuthMiddleware)

    # Mock get_settings to return our test settings
    with patch("src.auth.middleware.get_settings", return_value=mock_settings):
        yield app


@pytest_asyncio.fixture
async def client_with_middleware(
    app_with_middleware: Starlette,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client for app with middleware."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_middleware),
        base_url="http://test",
    ) as client:
        yield client


class TestAuthMiddleware:
    """Test AuthMiddleware behavior."""

    @pytest.mark.asyncio
    async def test_sets_claims_for_valid_token(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """Valid token results in request.state.auth_claims being set."""
        tenant_id = str(uuid.uuid4())
        token = create_dev_token(
            sub="user123",
            tenant_id=tenant_id,
            role="viewer",
            secret=TEST_SECRET,
            expires_in=3600,
        )

        response = await client_with_middleware.get(
            "/api/test",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is True
        assert data["sub"] == "user123"
        assert data["tenant_id"] == tenant_id

    @pytest.mark.asyncio
    async def test_sets_none_for_missing_token(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """Request without Authorization header gets auth_claims=None."""
        response = await client_with_middleware.get("/api/test")

        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is False

    @pytest.mark.asyncio
    async def test_sets_none_for_invalid_token(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """Invalid token results in auth_claims=None (doesn't block request)."""
        response = await client_with_middleware.get(
            "/api/test",
            headers={"Authorization": "Bearer invalid-garbage-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is False

    @pytest.mark.asyncio
    async def test_sets_none_for_expired_token(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """Expired token results in auth_claims=None."""
        token = create_dev_token(
            sub="user123",
            tenant_id=str(uuid.uuid4()),
            secret=TEST_SECRET,
            expires_in=-3600,  # Already expired
        )

        response = await client_with_middleware.get(
            "/api/test",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is False

    @pytest.mark.asyncio
    async def test_sets_none_for_malformed_header(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """Authorization header without 'Bearer ' prefix is ignored."""
        response = await client_with_middleware.get(
            "/api/test",
            headers={"Authorization": "NotBearer some-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is False


class TestPublicPathSkipping:
    """Test middleware skips public paths."""

    @pytest.mark.asyncio
    async def test_skips_health_endpoint(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """/health endpoint bypasses auth middleware."""
        # No token provided
        response = await client_with_middleware.get("/health")

        assert response.status_code == 200
        # Should succeed even without auth

    @pytest.mark.asyncio
    async def test_skips_docs_endpoint(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """/docs endpoint bypasses auth middleware."""
        response = await client_with_middleware.get("/docs")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_skips_openapi_endpoint(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """/openapi.json endpoint bypasses auth middleware."""
        response = await client_with_middleware.get("/openapi.json")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_does_not_skip_api_endpoints(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """API endpoints go through middleware."""
        # Without token, middleware sets auth_claims=None
        response = await client_with_middleware.get("/api/test")

        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is False


class TestMiddlewareLogging:
    """Test middleware logging behavior."""

    @pytest.mark.asyncio
    async def test_logs_successful_validation(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """Middleware logs successful token validation."""
        tenant_id = str(uuid.uuid4())
        token = create_dev_token(
            sub="user456",
            tenant_id=tenant_id,
            secret=TEST_SECRET,
        )

        with patch("src.auth.middleware.log") as mock_log:
            response = await client_with_middleware.get(
                "/api/test",
                headers={"Authorization": f"Bearer {token}"},
            )

            assert response.status_code == 200

            # Should log debug message on success
            mock_log.debug.assert_called_once()
            call_args = mock_log.debug.call_args
            assert "auth.token_validated" in call_args[0]

    @pytest.mark.asyncio
    async def test_logs_validation_failure(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """Middleware logs token validation errors."""
        with patch("src.auth.middleware.log") as mock_log:
            response = await client_with_middleware.get(
                "/api/test",
                headers={"Authorization": "Bearer invalid-token"},
            )

            assert response.status_code == 200

            # Should log warning on failure
            mock_log.warning.assert_called_once()
            call_args = mock_log.warning.call_args
            assert "auth.token_invalid" in call_args[0]


class TestMiddlewareIntegration:
    """Integration tests for middleware with request flow."""

    @pytest.mark.asyncio
    async def test_multiple_requests_with_different_tokens(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """Middleware correctly handles different tokens across requests."""
        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())

        token_a = create_dev_token(
            sub="user_a",
            tenant_id=tenant_a,
            secret=TEST_SECRET,
        )
        token_b = create_dev_token(
            sub="user_b",
            tenant_id=tenant_b,
            secret=TEST_SECRET,
        )

        # First request with token A
        response_a = await client_with_middleware.get(
            "/api/test",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert response_a.status_code == 200
        data_a = response_a.json()
        assert data_a["sub"] == "user_a"
        assert data_a["tenant_id"] == tenant_a

        # Second request with token B
        response_b = await client_with_middleware.get(
            "/api/test",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert response_b.status_code == 200
        data_b = response_b.json()
        assert data_b["sub"] == "user_b"
        assert data_b["tenant_id"] == tenant_b

        # Third request without token
        response_none = await client_with_middleware.get("/api/test")
        assert response_none.status_code == 200
        data_none = response_none.json()
        assert data_none["authenticated"] is False

    @pytest.mark.asyncio
    async def test_middleware_does_not_block_request_on_auth_failure(
        self,
        client_with_middleware: AsyncClient,
    ) -> None:
        """Middleware doesn't raise 401 - lets dependencies handle it."""
        # Invalid token should not result in 401 from middleware
        response = await client_with_middleware.get(
            "/api/test",
            headers={"Authorization": "Bearer garbage"},
        )

        # Middleware sets auth_claims=None and lets request proceed
        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is False
