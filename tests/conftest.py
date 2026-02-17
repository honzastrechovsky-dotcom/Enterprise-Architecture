"""
Shared test fixtures for pytest.

Provides common mocks and test data for all test modules:
- fake_settings: Test environment configuration
- mock_db_session: Async database session mock
- db_session: Real async session fixture (uses mock for practicality)
- client: Async HTTP client for testing FastAPI app
- auth_headers: Valid JWT Bearer token headers
- test_tenant_id, test_user_id: Fixed UUIDs for consistent testing
- tenant_a, tenant_b: Multi-tenant test IDs
- viewer_user_a, admin_user_a, admin_user_b: User fixtures for different roles
- client_viewer_a, client_admin_a, client_admin_b: Pre-authenticated HTTP clients
- make_token: Helper to create test JWT tokens
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession

import socket

from src.config import Environment, Settings, get_settings
from src.models.user import User, UserRole


# ------------------------------------------------------------------ #
# Session-scoped: clear settings cache between test sessions
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True, scope="session")
def _clear_settings_cache():
    """Clear the lru_cache on get_settings so test overrides take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ------------------------------------------------------------------ #
# Auto-skip integration tests when DB is unavailable
# ------------------------------------------------------------------ #

def _db_is_available() -> bool:
    """Return True if PostgreSQL is reachable on localhost:5432."""
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


_DB_AVAILABLE: bool | None = None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip tests marked ``@pytest.mark.integration`` when DB is down."""
    global _DB_AVAILABLE  # noqa: PLW0603
    if _DB_AVAILABLE is None:
        _DB_AVAILABLE = _db_is_available()

    if _DB_AVAILABLE:
        return

    skip_marker = pytest.mark.skip(reason="Database unavailable, skipping integration test")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


# ------------------------------------------------------------------ #
# Constants for Test JWTs
# ------------------------------------------------------------------ #

TEST_JWT_SECRET = "dev-only-jwt-secret-not-for-production"


def make_token(
    sub: str,
    tenant_id: str,
    role: str = "viewer",
    email: str = "test@example.com",
) -> str:
    """Create a test JWT token using HS256.

    Args:
        sub: Subject claim (user's external_id)
        tenant_id: Tenant UUID as string
        role: User role (admin, operator, viewer)
        email: User email address

    Returns:
        Encoded JWT token string
    """
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": sub,
        "tenant_id": tenant_id,
        "role": role,
        "email": email,
        "aud": "enterprise-agents-api",
        "iat": now,
        "exp": now + 3600,  # 1 hour expiry
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")


# ------------------------------------------------------------------ #
# Settings & App Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def fake_settings() -> Settings:
    """Test environment settings with safe defaults."""
    return Settings(
        environment=Environment.TEST,
        secret_key="test-secret-key",
        database_url="postgresql+asyncpg://test:test@localhost:5432/test_db",
        litellm_base_url="http://localhost:4000",
        litellm_api_key="sk-test-key",
        oidc_issuer_url="http://localhost:8080/realms/test",
        dev_jwt_secret=TEST_JWT_SECRET,
        redis_url="redis://localhost:6379/1",
        debug=True,
        db_echo_sql=False,
        rate_limit_per_minute=100,
    )


@pytest.fixture
def test_app(fake_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Create FastAPI test app instance with test settings.

    This overrides the get_settings dependency to use test configuration.
    The app is created fresh for each test to ensure isolation.
    Also overrides get_db_session to avoid requiring a real DB connection.
    """
    from src.config import get_settings
    from src.database import get_db_session
    from src.main import create_app

    # Clear the lru_cache so get_settings returns our fake_settings
    get_settings.cache_clear()

    # Mock get_settings to return our test settings
    monkeypatch.setattr("src.config.get_settings", lambda: fake_settings)

    # Create app with test settings
    app = create_app()

    # Override get_db_session to use a mock session (no real DB required).
    # The execute() return value is a MagicMock so that synchronous result
    # methods like .scalar(), .scalar_one(), and .scalar_one_or_none() return
    # plain values rather than coroutines (which AsyncMock would produce).
    async def mock_get_db_session() -> AsyncGenerator[AsyncSession, None]:
        mock = AsyncMock(spec=AsyncSession)
        mock.execute = AsyncMock()
        result_mock = MagicMock()  # Synchronous result object
        result_mock.scalars.return_value.all.return_value = []
        result_mock.scalars.return_value.first.return_value = None
        result_mock.scalar_one_or_none.return_value = None
        result_mock.scalar_one.return_value = None
        result_mock.scalar.return_value = None
        result_mock.all.return_value = []
        mock.execute.return_value = result_mock
        mock.commit = AsyncMock()
        mock.rollback = AsyncMock()
        mock.flush = AsyncMock()
        mock.close = AsyncMock()
        mock.delete = MagicMock()
        yield mock

    app.dependency_overrides[get_db_session] = mock_get_db_session

    # Override get_current_user so that the mock DB doesn't need to return
    # a real User row.  We build an AuthenticatedUser straight from JWT
    # claims, honouring the role encoded in the token.
    from src.auth.dependencies import AuthenticatedUser, get_current_user
    from src.auth.oidc import validate_token

    async def _mock_get_current_user(
        request: Request,
        db: AsyncSession = Depends(get_db_session),
        settings: Settings = Depends(get_settings),
    ) -> AuthenticatedUser:
        from fastapi import HTTPException as _H
        from src.auth.oidc import TokenValidationError
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise _H(status_code=401, detail="Missing auth")
        token = auth_header.removeprefix("Bearer ").strip()
        try:
            claims = await validate_token(token, settings)
        except TokenValidationError:
            raise _H(status_code=401, detail="Invalid token")
        tenant_id = uuid.UUID(claims["tenant_id"])
        role_map = {"admin": UserRole.ADMIN, "operator": UserRole.OPERATOR, "viewer": UserRole.VIEWER}
        user = User(
            id=uuid.uuid5(uuid.NAMESPACE_URL, claims["sub"]),
            tenant_id=tenant_id,
            external_id=claims["sub"],
            email=claims.get("email", "test@test.com"),
            display_name=claims.get("name", "Test User"),
            role=role_map.get(claims.get("role", "viewer"), UserRole.VIEWER),
            is_active=True,
        )
        return AuthenticatedUser(user=user, claims=claims)

    app.dependency_overrides[get_current_user] = _mock_get_current_user

    return app


# ------------------------------------------------------------------ #
# HTTP Client Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
async def client(test_app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client for testing the FastAPI application.

    Uses httpx.AsyncClient with ASGITransport to test the app without
    spinning up a real HTTP server.
    """
    transport = httpx.ASGITransport(app=test_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as ac:
        yield ac


# ------------------------------------------------------------------ #
# Database Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def mock_db_session() -> AsyncSession:
    """Mock async database session for unit tests.

    Use this fixture when testing business logic that doesn't require
    real database interactions. Provides an AsyncMock with common
    SQLAlchemy session methods.

    The execute() return value is a MagicMock so that synchronous result
    methods like .scalar(), .scalar_one(), and .scalar_one_or_none() return
    plain values rather than coroutines (which AsyncMock would produce).
    """
    mock = AsyncMock(spec=AsyncSession)
    mock.execute = AsyncMock()
    mock.execute.return_value = MagicMock()  # Synchronous result object
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    mock.flush = AsyncMock()
    mock.close = AsyncMock()
    mock.delete = MagicMock()  # Synchronous delete
    return mock


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Real async database session fixture.

    For integration tests. Currently returns a mock for practicality
    since we don't spin up PostgreSQL in the test environment.

    In a full integration test setup, this would:
    1. Create a test database
    2. Run migrations
    3. Yield a real session
    4. Rollback and cleanup

    The execute() return value is a MagicMock so that synchronous result
    methods like .scalar(), .scalar_one(), and .scalar_one_or_none() return
    plain values rather than coroutines (which AsyncMock would produce).
    """
    # Uses a mock session; wire a real DB session for full integration test coverage
    mock = AsyncMock(spec=AsyncSession)
    mock.execute = AsyncMock()
    mock.execute.return_value = MagicMock()  # Synchronous result object
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    mock.close = AsyncMock()
    mock.delete = MagicMock()

    # Track added objects and auto-assign UUIDs on flush
    _added_objects: list[Any] = []

    def _mock_add(obj: Any) -> None:
        _added_objects.append(obj)

    async def _mock_flush(*args: Any, **kwargs: Any) -> None:
        for obj in _added_objects:
            if hasattr(obj, "id") and obj.id is None:
                obj.id = uuid.uuid4()
        # Don't clear - objects may need IDs on subsequent flushes

    mock.add = _mock_add
    mock.flush = _mock_flush

    yield mock

    # Cleanup would happen here
    await mock.close()


# ------------------------------------------------------------------ #
# Tenant & User ID Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def tenant_a() -> SimpleNamespace:
    """Tenant A for multi-tenant tests. Exposes .id as string UUID."""
    return SimpleNamespace(id="12345678-1234-5678-1234-567812345678")


@pytest.fixture
def tenant_b() -> SimpleNamespace:
    """Tenant B for multi-tenant tests. Exposes .id as string UUID."""
    return SimpleNamespace(id="87654321-8765-4321-8765-432187654321")


@pytest.fixture
def test_tenant_id(tenant_a: SimpleNamespace) -> uuid.UUID:
    """Fixed tenant UUID for consistent testing (defaults to tenant_a)."""
    return uuid.UUID(tenant_a.id)


@pytest.fixture
def test_user_id() -> uuid.UUID:
    """Fixed user UUID for consistent testing."""
    return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


# ------------------------------------------------------------------ #
# User Fixtures (Dict Format for JWT Claims)
# ------------------------------------------------------------------ #

@pytest.fixture
def viewer_user_a(tenant_a: SimpleNamespace) -> SimpleNamespace:
    """Viewer user in tenant A.

    Exposes .id (sub), .tenant_id, .role, .email as attributes.
    """
    return SimpleNamespace(
        id="viewer-a-external-id",
        sub="viewer-a-external-id",
        tenant_id=tenant_a.id,
        role="viewer",
        email="viewer.a@example.com",
    )


@pytest.fixture
def admin_user_a(tenant_a: SimpleNamespace) -> SimpleNamespace:
    """Admin user in tenant A.

    Exposes .id (sub), .tenant_id, .role, .email as attributes.
    """
    return SimpleNamespace(
        id="admin-a-external-id",
        sub="admin-a-external-id",
        tenant_id=tenant_a.id,
        role="admin",
        email="admin.a@example.com",
    )


@pytest.fixture
def admin_user_b(tenant_b: SimpleNamespace) -> SimpleNamespace:
    """Admin user in tenant B.

    Exposes .id (sub), .tenant_id, .role, .email as attributes.
    """
    return SimpleNamespace(
        id="admin-b-external-id",
        sub="admin-b-external-id",
        tenant_id=tenant_b.id,
        role="admin",
        email="admin.b@example.com",
    )


# ------------------------------------------------------------------ #
# Pre-Authenticated HTTP Client Fixtures
# ------------------------------------------------------------------ #

def _user_token(user: SimpleNamespace) -> str:
    """Create a JWT token from a user namespace fixture."""
    return make_token(
        sub=user.sub, tenant_id=user.tenant_id, role=user.role, email=user.email
    )


@pytest.fixture
async def client_viewer_a(
    test_app: FastAPI,
    viewer_user_a: SimpleNamespace,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client authenticated as viewer in tenant A."""
    token = _user_token(viewer_user_a)
    transport = httpx.ASGITransport(app=test_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def client_admin_a(
    test_app: FastAPI,
    admin_user_a: SimpleNamespace,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client authenticated as admin in tenant A."""
    token = _user_token(admin_user_a)
    transport = httpx.ASGITransport(app=test_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def client_admin_b(
    test_app: FastAPI,
    admin_user_b: SimpleNamespace,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client authenticated as admin in tenant B."""
    token = _user_token(admin_user_b)
    transport = httpx.ASGITransport(app=test_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


# ------------------------------------------------------------------ #
# Auth Helper Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def auth_headers(viewer_user_a: SimpleNamespace) -> dict[str, str]:
    """Valid JWT Bearer token authorization headers.

    Returns headers dict with Authorization header containing a valid
    test JWT token for viewer_user_a. For custom tokens, use make_token()
    directly.
    """
    token = _user_token(viewer_user_a)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def viewer_auth_headers(tenant_a: SimpleNamespace) -> dict[str, str]:
    """Auth headers for a viewer user."""
    token = make_token(sub="viewer-sub", tenant_id=tenant_a.id, role="viewer")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def operator_auth_headers(tenant_a: SimpleNamespace) -> dict[str, str]:
    """Auth headers for an operator user."""
    token = make_token(sub="operator-sub", tenant_id=tenant_a.id, role="operator")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_auth_headers(tenant_a: SimpleNamespace) -> dict[str, str]:
    """Auth headers for an admin user."""
    token = make_token(sub="admin-sub", tenant_id=tenant_a.id, role="admin")
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ #
# Model Instance Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def test_user(test_tenant_id: uuid.UUID, test_user_id: uuid.UUID) -> User:
    """Create test user model instance."""
    return User(
        id=test_user_id,
        tenant_id=test_tenant_id,
        external_id="test-external-id",
        email="test@example.com",
        display_name="Test User",
        role=UserRole.OPERATOR,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def test_admin_user(test_tenant_id: uuid.UUID) -> User:
    """Create test admin user model instance."""
    return User(
        id=uuid.uuid4(),
        tenant_id=test_tenant_id,
        external_id="admin-external-id",
        email="admin@example.com",
        display_name="Admin User",
        role=UserRole.ADMIN,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def test_viewer_user(test_tenant_id: uuid.UUID) -> User:
    """Create test viewer user model instance."""
    return User(
        id=uuid.uuid4(),
        tenant_id=test_tenant_id,
        external_id="viewer-external-id",
        email="viewer@example.com",
        display_name="Viewer User",
        role=UserRole.VIEWER,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ------------------------------------------------------------------ #
# Service Mocks
# ------------------------------------------------------------------ #

@pytest.fixture
def mock_audit_service():
    """Mock audit service for testing."""
    mock = AsyncMock()
    mock.log = AsyncMock()
    return mock
