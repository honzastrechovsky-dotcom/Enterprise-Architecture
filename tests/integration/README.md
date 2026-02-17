# Integration Tests

Integration tests for the Enterprise Agent Platform. These tests use **real database connections** and **real ASGI transport** to validate end-to-end functionality.

## Overview

Unlike unit tests (which mock database and external dependencies), integration tests:

- **Real Database**: SQLite in-memory by default, PostgreSQL when available
- **Real Middleware**: All FastAPI middleware (auth, CORS, metrics) is active
- **Real ASGI**: Uses `httpx.AsyncClient` with `ASGITransport` (not mocked)
- **Seeded Data**: Pre-populated tenants and users for realistic scenarios
- **Mocked LLMs**: External LLM calls are mocked to avoid API costs

## Running Tests

### Run All Integration Tests

```bash
pytest -m integration tests/integration/
```

### Run Specific Test File

```bash
pytest -m integration tests/integration/test_api_flow.py
```

### Run with Verbose Output

```bash
pytest -m integration -v tests/integration/
```

### Run Only Unit Tests (Exclude Integration)

```bash
pytest -m "not integration"
```

## Database Configuration

Integration tests support multiple database backends:

### 1. SQLite In-Memory (Default)

No setup required. Tests use `sqlite+aiosqlite:///:memory:` automatically.

**Pros**: Fast, no external dependencies
**Cons**: No PostgreSQL-specific features (pgvector, full-text search)

### 2. PostgreSQL (Local Docker)

Set environment variable to use local PostgreSQL from docker-compose:

```bash
export DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents"
pytest -m integration tests/integration/
```

Tests will create a separate test database (`enterprise_agents_test`).

### 3. PostgreSQL (CI/CD)

For CI/CD pipelines with a dedicated test database:

```bash
export TESTING_DATABASE_URL="postgresql+asyncpg://user:pass@test-db:5432/test_db"
pytest -m integration tests/integration/
```

## Test Structure

```
tests/integration/
├── __init__.py                  # Package marker
├── conftest.py                  # Integration test fixtures
├── test_api_flow.py             # API endpoints and flows
├── test_auth_flow.py            # Authentication and authorization
├── test_plugin_flow.py          # Plugin management
└── test_analytics_flow.py       # Analytics and metrics
```

## Key Fixtures

### Database Fixtures

- `integration_settings`: Test settings with real DB URL
- `integration_engine`: SQLAlchemy async engine (session-scoped)
- `integration_db`: Async session for single test (transaction rollback)

### App Fixtures

- `integration_app`: FastAPI app with real database
- `integration_client`: HTTP client with real ASGI transport

### Seed Data

- `seed_data`: Pre-populated database with:
  - 2 tenants: `tenant_a`, `tenant_b`
  - 3 users: `admin_a`, `viewer_a`, `admin_b`

### Authenticated Clients

- `client_admin_a_int`: HTTP client authenticated as admin in tenant A
- `client_viewer_a_int`: HTTP client authenticated as viewer in tenant A
- `client_admin_b_int`: HTTP client authenticated as admin in tenant B

### Mocks

- `mock_llm_client`: Mocked LiteLLM client (avoids real API calls)

## Coverage Areas

### API Flow Tests (`test_api_flow.py`)

- ✅ Full chat flow: create conversation → send message → get response → check history
- ✅ Conversation CRUD: create, list, get, update, delete
- ✅ Tenant isolation: admin_a creates resource, admin_b cannot see it
- ✅ RBAC: viewer cannot access admin endpoints
- ✅ Health endpoints return 200
- ✅ Pagination support
- ✅ Message ordering

### Auth Flow Tests (`test_auth_flow.py`)

- ✅ Valid token grants access
- ✅ Expired token returns 401
- ✅ Missing token returns 401
- ✅ Invalid signature returns 401
- ✅ Wrong tenant returns 403
- ✅ Role escalation prevention
- ✅ Token without required claims returns 401
- ✅ Public endpoints do not require auth
- ✅ Concurrent requests with same token

### Plugin Flow Tests (`test_plugin_flow.py`)

- ✅ Enable/disable plugin for tenant
- ✅ Plugin config CRUD
- ✅ Plugin list shows correct state per tenant
- ✅ Tenant isolation for plugins
- ✅ Viewer cannot modify plugins
- ✅ Plugin config validation
- ✅ State persistence

### Analytics Flow Tests (`test_analytics_flow.py`)

- ✅ Metrics middleware records API calls
- ✅ Analytics summary endpoint returns data
- ✅ Analytics endpoints require auth
- ✅ Tenant-specific analytics isolation
- ✅ Token usage tracking
- ✅ Response time metrics
- ✅ Date range filtering
- ✅ Export functionality

## Adding New Integration Tests

1. **Create test file** in `tests/integration/test_<feature>_flow.py`
2. **Mark with decorator**: `@pytest.mark.integration`
3. **Use fixtures**: Import from `conftest.py`
4. **Follow patterns**: See existing tests for structure

Example:

```python
import pytest
import httpx

@pytest.mark.integration
async def test_new_feature(
    client_admin_a_int: httpx.AsyncClient,
    seed_data: dict,
):
    """Test description."""
    resp = await client_admin_a_int.get("/api/v1/new-feature")
    assert resp.status_code == 200
```

## CI/CD Integration

### GitHub Actions Example

```yaml
- name: Run Integration Tests
  env:
    TESTING_DATABASE_URL: postgresql+asyncpg://test:test@postgres:5432/test_db
  run: |
    pytest -m integration --cov=src tests/integration/
```

### Docker Compose for Tests

```yaml
services:
  test-db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: test
      POSTGRES_PASSWORD: test
      POSTGRES_DB: test_db
```

## Troubleshooting

### Tests Fail with "Database not initialized"

Ensure fixtures are properly scoped:
- `integration_engine`: `scope="session"`
- `integration_db`: `scope="function"`

### SQLite Foreign Key Violations

SQLite requires foreign keys to be explicitly enabled. The `integration_engine` fixture handles this automatically.

### Slow Tests

Integration tests are slower than unit tests due to real database I/O. To speed up:

1. Use SQLite in-memory for local development
2. Use PostgreSQL only for CI/CD
3. Run integration tests separately from unit tests

### Connection Pool Exhausted

Tests use `NullPool` to avoid connection sharing. Each test gets a fresh connection.

## Best Practices

1. **Isolation**: Each test should be independent (use transaction rollback)
2. **Cleanup**: Fixtures handle cleanup automatically (rollback, drop tables)
3. **Realistic Data**: Use `seed_data` fixture for realistic multi-tenant scenarios
4. **Mock External**: Mock LLM calls, external APIs (not database)
5. **Test Real Flows**: Test complete user journeys, not isolated units

## Performance

Typical execution times (on modern hardware):

- **SQLite in-memory**: ~5-10 seconds for full suite
- **PostgreSQL (local)**: ~10-20 seconds for full suite
- **PostgreSQL (CI)**: ~15-30 seconds (includes container startup)

## Future Enhancements

- [ ] Add performance/load tests (simulate concurrent users)
- [ ] Add browser-based E2E tests (Playwright)
- [ ] Add chaos testing (network failures, DB crashes)
- [ ] Add cross-database compatibility tests (SQLite vs PostgreSQL)
