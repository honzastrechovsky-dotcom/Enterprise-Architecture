"""Integration test fixtures.

Provides real database sessions, seeded data, and authenticated clients
for end-to-end API testing.

Key Fixtures:
- integration_db: Real async DB session (SQLite in-memory or PostgreSQL)
- integration_app: FastAPI app with real DB and real middleware
- integration_client: HTTP client with real ASGI transport
- seed_data: Pre-populated tenants and users in the database
- client_admin_a_int, client_viewer_a_int, client_admin_b_int: Pre-authenticated clients
"""

import asyncio
import os
import uuid
from typing import AsyncGenerator

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from src.config import Environment, Settings, get_settings
from src.database import Base, init_db, close_db, get_db_session
from src.main import create_app
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from tests.conftest import make_token


# ------------------------------------------------------------------ #
# Test Database Configuration
# ------------------------------------------------------------------ #

def get_integration_db_url() -> str:
    """Get database URL for integration tests.

    Priority:
    1. TESTING_DATABASE_URL env var (for CI/CD with real PostgreSQL)
    2. DATABASE_URL env var (for local docker-compose)
    3. SQLite in-memory (fallback for pure unit test runs)

    SQLite requires special handling for foreign keys and async support.
    """
    # Check for explicit test database
    test_url = os.getenv("TESTING_DATABASE_URL")
    if test_url:
        return test_url

    # Check for regular database URL (docker-compose)
    db_url = os.getenv("DATABASE_URL")
    if db_url and "postgresql" in db_url:
        # Use postgres but add a test database suffix
        return db_url.replace("/enterprise_agents", "/enterprise_agents_test")

    # Fallback to SQLite in-memory (no persistence needed for tests)
    return "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    """Test settings for integration tests.

    Uses real database URL but disables external services (OIDC, telemetry).
    """
    db_url = get_integration_db_url()

    return Settings(
        environment=Environment.TEST,
        database_url=db_url,
        secret_key="integration-test-secret-key",
        dev_jwt_secret="test-jwt-secret-for-unit-tests",
        debug=True,
        db_echo_sql=False,  # Set to True for debugging SQL queries
        rate_limit_per_minute=1000,  # High limit for tests
        enable_telemetry=False,
        litellm_base_url="http://localhost:4000",
        litellm_api_key="sk-test-key",
        oidc_issuer_url="http://localhost:8080/realms/test",
        redis_url="redis://localhost:6379/15",  # Use DB 15 for tests
    )


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session.

    Pytest-asyncio requires this for session-scoped async fixtures.
    """
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def integration_engine(integration_settings: Settings) -> AsyncGenerator[AsyncEngine, None]:
    """Create database engine for integration tests.

    Session-scoped: created once, reused for all tests.
    Uses NullPool to avoid connection sharing issues between tests.
    """
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(
        integration_settings.database_url,
        echo=integration_settings.db_echo_sql,
        poolclass=NullPool,  # Each test gets fresh connection
        future=True,
    )

    # Enable foreign keys for SQLite (no-op for PostgreSQL)
    if "sqlite" in integration_settings.database_url:
        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Drop all tables and dispose engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def integration_db(integration_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional database session for a single test.

    Each test runs in an isolated transaction that is rolled back,
    ensuring tests don't interfere with each other.

    This is function-scoped: each test gets its own session.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(
        integration_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=True,
        autocommit=False,
    )

    async with session_factory() as session:
        # Start a transaction
        async with session.begin():
            yield session
            # Rollback happens automatically when context exits
            await session.rollback()


# ------------------------------------------------------------------ #
# FastAPI App with Real Database
# ------------------------------------------------------------------ #

@pytest.fixture
async def integration_app(integration_settings: Settings, integration_engine: AsyncEngine, monkeypatch):
    """Create FastAPI app with real database for integration testing.

    This app:
    - Uses real database (not mocked)
    - Uses real middleware (auth, CORS, metrics)
    - Uses real ASGI transport (via httpx.AsyncClient)
    - Mocks only external LLM calls
    """
    # Clear settings cache
    get_settings.cache_clear()

    # Override get_settings to return test settings
    monkeypatch.setattr("src.config.get_settings", lambda: integration_settings)

    # Initialize database with test settings
    init_db(integration_settings, for_test=True)

    # Create app
    app = create_app()

    # Override the get_db_session dependency to use our test engine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    test_session_factory = async_sessionmaker(
        integration_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_session] = override_get_db

    yield app

    # Cleanup
    await close_db()


@pytest.fixture
async def integration_client(integration_app) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client for integration tests with real ASGI transport.

    This client makes real HTTP requests through the ASGI stack,
    exercising all middleware and authentication logic.
    """
    transport = httpx.ASGITransport(app=integration_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client


# ------------------------------------------------------------------ #
# Seed Data Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
async def tenant_ids() -> dict[str, str]:
    """Fixed tenant IDs for consistent testing."""
    return {
        "tenant_a": "12345678-1234-5678-1234-567812345678",
        "tenant_b": "87654321-8765-4321-8765-432187654321",
    }


@pytest.fixture
async def seed_data(integration_db: AsyncSession, tenant_ids: dict[str, str]) -> dict:
    """Seed database with tenants and users for integration tests.

    Creates:
    - 2 tenants (tenant_a, tenant_b)
    - 3 users:
        - admin_a: Admin in tenant_a
        - viewer_a: Viewer in tenant_a
        - admin_b: Admin in tenant_b

    Returns dict with tenant and user objects for test access.
    """
    # Create tenants
    tenant_a = Tenant(
        id=uuid.UUID(tenant_ids["tenant_a"]),
        name="Tenant A Corp",
        slug="tenant-a",
        is_active=True,
    )
    tenant_b = Tenant(
        id=uuid.UUID(tenant_ids["tenant_b"]),
        name="Tenant B Industries",
        slug="tenant-b",
        is_active=True,
    )
    integration_db.add(tenant_a)
    integration_db.add(tenant_b)
    await integration_db.flush()

    # Create users
    admin_a = User(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        external_id="admin-a-external-id",
        email="admin.a@example.com",
        display_name="Admin A",
        role=UserRole.ADMIN,
        is_active=True,
    )
    viewer_a = User(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        external_id="viewer-a-external-id",
        email="viewer.a@example.com",
        display_name="Viewer A",
        role=UserRole.VIEWER,
        is_active=True,
    )
    admin_b = User(
        id=uuid.uuid4(),
        tenant_id=tenant_b.id,
        external_id="admin-b-external-id",
        email="admin.b@example.com",
        display_name="Admin B",
        role=UserRole.ADMIN,
        is_active=True,
    )

    integration_db.add(admin_a)
    integration_db.add(viewer_a)
    integration_db.add(admin_b)
    await integration_db.commit()

    return {
        "tenants": {
            "tenant_a": tenant_a,
            "tenant_b": tenant_b,
        },
        "users": {
            "admin_a": admin_a,
            "viewer_a": viewer_a,
            "admin_b": admin_b,
        },
    }


# ------------------------------------------------------------------ #
# Pre-Authenticated Client Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
async def client_admin_a_int(
    integration_app,
    seed_data: dict,
    tenant_ids: dict[str, str],
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client authenticated as admin in tenant A."""
    admin_a = seed_data["users"]["admin_a"]
    token = make_token(
        sub=admin_a.external_id,
        tenant_id=tenant_ids["tenant_a"],
        role="admin",
        email=admin_a.email,
    )

    transport = httpx.ASGITransport(app=integration_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@pytest.fixture
async def client_viewer_a_int(
    integration_app,
    seed_data: dict,
    tenant_ids: dict[str, str],
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client authenticated as viewer in tenant A."""
    viewer_a = seed_data["users"]["viewer_a"]
    token = make_token(
        sub=viewer_a.external_id,
        tenant_id=tenant_ids["tenant_a"],
        role="viewer",
        email=viewer_a.email,
    )

    transport = httpx.ASGITransport(app=integration_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@pytest.fixture
async def client_admin_b_int(
    integration_app,
    seed_data: dict,
    tenant_ids: dict[str, str],
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client authenticated as admin in tenant B."""
    admin_b = seed_data["users"]["admin_b"]
    token = make_token(
        sub=admin_b.external_id,
        tenant_id=tenant_ids["tenant_b"],
        role="admin",
        email=admin_b.email,
    )

    transport = httpx.ASGITransport(app=integration_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


# ------------------------------------------------------------------ #
# Mock LLM Fixture (for tests that trigger LLM calls)
# ------------------------------------------------------------------ #

@pytest.fixture
def mock_llm_client(monkeypatch):
    """Mock LiteLLM client to avoid real API calls in integration tests.

    Provides fake but realistic responses for LLM calls.
    """
    from unittest.mock import AsyncMock

    mock_client = AsyncMock()

    # Mock chat completion
    async def mock_create(*args, **kwargs):
        return {
            "id": "chatcmpl-test-123",
            "object": "chat.completion",
            "created": 1234567890,
            "model": kwargs.get("model", "gpt-4o-mini"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "This is a test response from the mocked LLM.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 20,
                "total_tokens": 70,
            },
        }

    mock_client.chat.completions.create = mock_create

    # Mock embedding
    async def mock_embed(*args, **kwargs):
        return {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "embedding": [0.1] * 1536,  # Fake 1536-dim embedding
                    "index": 0,
                }
            ],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 10, "total_tokens": 10},
        }

    mock_client.embeddings.create = mock_embed

    # Patch the LLM service to use our mock
    monkeypatch.setattr("src.services.llm.get_llm_client", lambda: mock_client)

    return mock_client
