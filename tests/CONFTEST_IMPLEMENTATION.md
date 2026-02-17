# Conftest.py Implementation Summary

## Overview
Rewrote `tests/conftest.py` to include ALL fixtures required by integration tests, addressing the critical gaps found in the Opus architecture review.

## Implemented Fixtures

### 1. Constants & Helpers
- **`TEST_JWT_SECRET`**: Constant for symmetric JWT signing in tests
- **`make_token(sub, tenant_id, role, email)`**: Helper function to create test JWT tokens using HS256 algorithm

### 2. Settings & App
- **`fake_settings`**: Test Settings instance with safe defaults (environment=TEST, dev_jwt_secret=TEST_JWT_SECRET)
- **`test_app`**: FastAPI application factory with test settings override using monkeypatch

### 3. HTTP Clients
- **`client`**: Base AsyncClient with ASGITransport (unauthenticated)
- **`client_viewer_a`**: Pre-authenticated AsyncClient for viewer in tenant A
- **`client_admin_a`**: Pre-authenticated AsyncClient for admin in tenant A
- **`client_admin_b`**: Pre-authenticated AsyncClient for admin in tenant B

### 4. Database Sessions
- **`mock_db_session`**: AsyncMock session for unit tests (spec=AsyncSession)
- **`db_session`**: Async generator fixture for integration tests (currently mocked, with TODO for real DB)

### 5. Tenant & User IDs
- **`tenant_a`**: UUID string "12345678-1234-5678-1234-567812345678"
- **`tenant_b`**: UUID string "87654321-8765-4321-8765-432187654321"
- **`test_tenant_id`**: UUID object (defaults to tenant_a)
- **`test_user_id`**: UUID object "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

### 6. User Fixtures (JWT Claims Format)
- **`viewer_user_a`**: Dict with {sub, tenant_id=tenant_a, role=viewer, email}
- **`admin_user_a`**: Dict with {sub, tenant_id=tenant_a, role=admin, email}
- **`admin_user_b`**: Dict with {sub, tenant_id=tenant_b, role=admin, email}

### 7. Auth Helpers
- **`auth_headers`**: Function returning {"Authorization": "Bearer <token>"} for viewer_user_a

### 8. Model Instances
- **`test_user`**: User model instance (OPERATOR role)
- **`test_admin_user`**: User model instance (ADMIN role)
- **`test_viewer_user`**: User model instance (VIEWER role)

### 9. Service Mocks
- **`mock_audit_service`**: AsyncMock for audit service testing

## Key Design Decisions

### 1. JWT Token Generation
- Uses `jwt.encode()` with HS256 algorithm (symmetric secret)
- Matches the dev mode validation in `src/auth/oidc.py`
- Includes all required claims: sub, tenant_id, role, aud, iat, exp, jti
- Audience hardcoded to "enterprise-agents-api" matching OIDC config

### 2. Settings Override Pattern
```python
@pytest.fixture
def test_app(fake_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from src.config import get_settings
    from src.main import create_app

    get_settings.cache_clear()  # Clear lru_cache
    monkeypatch.setattr("src.config.get_settings", lambda: fake_settings)
    return create_app()
```

This ensures:
- Test settings are used instead of environment variables
- Each test gets a fresh app instance
- No cross-test pollution

### 3. Pre-Authenticated Clients
```python
@pytest.fixture
async def client_viewer_a(test_app: FastAPI, viewer_user_a: dict[str, Any]):
    token = make_token(**viewer_user_a)
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac
```

Benefits:
- Tests don't need to manually create tokens
- Simulates real authenticated requests
- Easy multi-tenant testing (client_admin_a vs client_admin_b)

### 4. Database Session Strategy
Currently uses AsyncMock for both `mock_db_session` and `db_session` because:
- No PostgreSQL running in test environment
- Tests can run without external dependencies
- Fixture NAMES exist so pytest collection succeeds

Future enhancement (TODO in code):
```python
async def db_session():
    # 1. Create test database
    # 2. Run migrations
    # 3. Yield real AsyncSession
    # 4. Rollback and cleanup
```

### 5. Multi-Tenant Testing Support
- `tenant_a` and `tenant_b` enable cross-tenant isolation tests
- `admin_user_a` and `admin_user_b` verify tenant boundaries
- Pre-authenticated clients make it easy to test authorization

## Usage Examples

### Basic Authenticated Request
```python
async def test_list_users(client_admin_a):
    response = await client_admin_a.get("/api/v1/users")
    assert response.status_code == 200
```

### Custom Token
```python
def test_custom_token(client, make_token):
    token = make_token(sub="custom-user", tenant_id="...", role="operator")
    response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
```

### Multi-Tenant Isolation
```python
async def test_tenant_isolation(client_admin_a, client_admin_b):
    # Admin A creates resource in tenant A
    resp_a = await client_admin_a.post("/api/v1/resources", json={...})
    resource_id = resp_a.json()["id"]

    # Admin B should NOT see tenant A's resource
    resp_b = await client_admin_b.get(f"/api/v1/resources/{resource_id}")
    assert resp_b.status_code == 404
```

### Database Mocking
```python
async def test_user_repository(mock_db_session):
    mock_db_session.execute.return_value.scalar_one_or_none.return_value = User(...)
    repo = UserRepository(mock_db_session)
    user = await repo.get_by_email("test@example.com")
    assert user is not None
```

## Dependencies

Required imports in conftest.py:
```python
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Environment, Settings
from src.models.user import User, UserRole
```

## Validation

To verify fixtures are properly recognized:
```bash
# Install dev dependencies first
pip install -e ".[dev]"

# Collect tests to verify no fixture errors
pytest --collect-only tests/

# Run specific test file
pytest tests/test_auth.py -v
```

## Changes from Original conftest.py

### Added:
- `TEST_JWT_SECRET` constant
- `make_token()` helper function
- `test_app` fixture (FastAPI app factory)
- `client` fixture (base AsyncClient)
- `tenant_a`, `tenant_b` fixtures
- `viewer_user_a`, `admin_user_a`, `admin_user_b` fixtures (dict format)
- `client_viewer_a`, `client_admin_a`, `client_admin_b` fixtures (pre-authenticated)
- `db_session` fixture (async generator)
- `test_viewer_user` fixture (User model instance)

### Modified:
- `fake_settings`: Added `dev_jwt_secret=TEST_JWT_SECRET` to match test token signing
- `auth_headers`: Now generates real JWT token instead of placeholder string
- `test_user`: Added `external_id` field (required by User model)
- `test_admin_user`: Added `external_id` field

### Preserved:
- All existing fixtures that were working correctly
- Docstrings and type hints
- Section organization with comment headers

## Next Steps

1. **Install dependencies**: `pip install -e ".[dev]"`
2. **Verify collection**: `pytest --collect-only`
3. **Run tests**: `pytest tests/ -v`
4. **Add real DB session** (optional): Replace `db_session` mock with real PostgreSQL test database when integration tests need it

## Notes

- All fixture names match what integration tests expect (verified against Opus review)
- JWT tokens use the same algorithm and claims as `src/auth/oidc.py` dev mode
- Settings override pattern ensures test isolation
- Pre-authenticated clients reduce test boilerplate
- Mock database sessions allow tests to run without PostgreSQL
