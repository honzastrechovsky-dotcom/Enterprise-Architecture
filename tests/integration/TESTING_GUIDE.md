# Integration Testing Guide

Complete guide to setting up and running integration tests for the Enterprise Agent Platform.

## Prerequisites

### 1. Install Dependencies

```bash
# Create virtual environment (if not exists)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install project with dev dependencies
pip install -e ".[dev]"
```

### 2. Install SQLite Support (for in-memory tests)

```bash
pip install aiosqlite
```

The project already includes `aiosqlite` in the dependencies, but ensure it's installed.

## Quick Start

### Run All Integration Tests (SQLite)

```bash
# Fastest option - uses in-memory SQLite
make test-integration-sqlite

# Or directly with pytest
pytest -m integration tests/integration/ -v
```

### Run All Integration Tests (PostgreSQL)

```bash
# Start PostgreSQL via docker-compose
docker-compose up -d db

# Wait for DB to be ready (about 10 seconds)
sleep 10

# Run tests against PostgreSQL
make test-integration-postgres

# Or set env var manually
export DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents"
pytest -m integration tests/integration/ -v
```

### Run Specific Test File

```bash
pytest -m integration tests/integration/test_api_flow.py -v
```

### Run Specific Test

```bash
pytest -m integration tests/integration/test_api_flow.py::test_health_endpoints_return_200 -v
```

## Test Execution Modes

### Mode 1: SQLite In-Memory (Default - Fastest)

**Best for**: Local development, CI/CD

```bash
pytest -m integration tests/integration/
```

**Database**: `sqlite+aiosqlite:///:memory:`

**Pros**:
- No external dependencies
- Fast (5-10 seconds for full suite)
- Automatic cleanup (in-memory)

**Cons**:
- No PostgreSQL-specific features
- No pgvector support
- No concurrent access testing

### Mode 2: PostgreSQL (Local Docker)

**Best for**: Testing real database features, realistic scenarios

```bash
# Start services
docker-compose up -d db redis

# Run tests
export DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents"
pytest -m integration tests/integration/
```

**Database**: PostgreSQL 16 with pgvector

**Pros**:
- Real PostgreSQL features
- pgvector support
- Realistic performance
- Concurrent access testing

**Cons**:
- Requires Docker
- Slower (15-20 seconds)
- Requires cleanup between runs

### Mode 3: Dedicated Test Database (CI/CD)

**Best for**: CI/CD pipelines, isolated test environments

```bash
export TESTING_DATABASE_URL="postgresql+asyncpg://user:pass@test-db:5432/test_db"
pytest -m integration tests/integration/
```

**Database**: Separate test database (auto-created)

**Pros**:
- Isolated from dev database
- No data conflicts
- Safe for parallel test runs

## Verifying Integration Test Setup

### Step 1: Verify Fixtures Load

```bash
pytest --collect-only tests/integration/
```

**Expected output**:
```
<Module test_api_flow.py>
  <Function test_health_endpoints_return_200>
  <Function test_full_chat_flow>
  ...
<Module test_auth_flow.py>
  <Function test_valid_token_grants_access>
  ...
```

### Step 2: Run Health Check Test

```bash
pytest -m integration tests/integration/test_api_flow.py::test_health_endpoints_return_200 -v
```

**Expected output**:
```
tests/integration/test_api_flow.py::test_health_endpoints_return_200 PASSED [100%]
```

### Step 3: Run Full API Flow Test

```bash
pytest -m integration tests/integration/test_api_flow.py::test_full_chat_flow -v
```

**Expected output**:
```
tests/integration/test_api_flow.py::test_full_chat_flow PASSED [100%]
```

### Step 4: Run All Integration Tests

```bash
pytest -m integration tests/integration/ -v
```

**Expected**: All tests pass (or skip if endpoints not implemented yet)

## Test Coverage Report

### Generate Coverage Report

```bash
pytest -m integration tests/integration/ --cov=src --cov-report=term-missing
```

**Expected coverage areas**:
- `src/api/` - API endpoints
- `src/auth/` - Authentication middleware
- `src/models/` - ORM models
- `src/services/` - Business logic
- `src/middleware/` - Metrics, security

### Generate HTML Coverage Report

```bash
pytest -m integration tests/integration/ --cov=src --cov-report=html
open htmlcov/index.html  # View in browser
```

## Debugging Integration Tests

### Enable SQL Query Logging

```python
# In tests/integration/conftest.py, modify integration_settings:
@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    return Settings(
        ...
        db_echo_sql=True,  # Enable SQL logging
        ...
    )
```

Then run tests:
```bash
pytest -m integration tests/integration/ -v -s
```

### Enable Verbose HTTP Logging

```bash
pytest -m integration tests/integration/ -v -s --log-cli-level=DEBUG
```

### Drop into Debugger on Failure

```bash
pytest -m integration tests/integration/ --pdb
```

### Run Single Test with Full Output

```bash
pytest -m integration tests/integration/test_api_flow.py::test_full_chat_flow -vvs
```

## Common Issues and Solutions

### Issue: "Database not initialized"

**Cause**: Fixtures not properly scoped

**Solution**: Ensure fixtures are used correctly:
```python
@pytest.mark.integration
async def test_example(integration_db: AsyncSession):  # Correct
    ...
```

### Issue: "FOREIGN KEY constraint failed" (SQLite)

**Cause**: Foreign keys not enabled

**Solution**: Already handled in `conftest.py`. Verify:
```python
@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
```

### Issue: "Connection pool exhausted"

**Cause**: Too many concurrent connections

**Solution**: Already handled via `NullPool`. If persists, check for leaked connections.

### Issue: "404 Not Found" on endpoints

**Cause**: Endpoint not implemented yet

**Solution**: Test skips automatically:
```python
if resp.status_code == 404:
    pytest.skip("Endpoint not implemented yet")
```

### Issue: Tests pass locally but fail in CI

**Cause**: Timing issues, database differences

**Solution**:
1. Add explicit waits: `await asyncio.sleep(0.5)`
2. Use same database in CI (PostgreSQL)
3. Check CI logs for specific errors

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Integration Tests

on: [push, pull_request]

jobs:
  integration-tests:
    runs-on: ubuntu-latest

    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: test
          POSTGRES_PASSWORD: test
          POSTGRES_DB: test_db
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5432:5432

    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          pip install -e ".[dev]"

      - name: Run integration tests
        env:
          TESTING_DATABASE_URL: postgresql+asyncpg://test:test@localhost:5432/test_db
        run: |
          pytest -m integration tests/integration/ --cov=src --cov-report=xml

      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with:
          files: ./coverage.xml
```

### GitLab CI Example

```yaml
integration-tests:
  stage: test
  image: python:3.12
  services:
    - name: pgvector/pgvector:pg16
      alias: postgres
  variables:
    POSTGRES_DB: test_db
    POSTGRES_USER: test
    POSTGRES_PASSWORD: test
    TESTING_DATABASE_URL: postgresql+asyncpg://test:test@postgres:5432/test_db
  script:
    - pip install -e ".[dev]"
    - pytest -m integration tests/integration/ --cov=src --cov-report=term
  coverage: '/TOTAL.*\s+(\d+%)$/'
```

## Performance Benchmarks

Expected execution times on modern hardware (Intel i7, 16GB RAM):

| Configuration | Test Count | Duration | DB Type |
|---------------|-----------|----------|---------|
| SQLite (default) | ~30 tests | 5-8s | In-memory |
| PostgreSQL (local) | ~30 tests | 12-18s | Docker |
| PostgreSQL (CI) | ~30 tests | 20-30s | Remote |

## Test Data Cleanup

Integration tests automatically clean up via:

1. **Transaction Rollback**: Each test runs in a transaction that's rolled back
2. **Session Cleanup**: Database sessions are properly closed
3. **Table Drop**: Test tables are dropped after session ends

**No manual cleanup required.**

## Best Practices

1. **Isolation**: Each test is independent (no shared state)
2. **Fixtures**: Use provided fixtures (don't create custom DB sessions)
3. **Assertions**: Test real HTTP status codes and response bodies
4. **Mocking**: Mock external services (LLM), not database
5. **Skip**: Skip tests for unimplemented endpoints:
   ```python
   if resp.status_code == 404:
       pytest.skip("Endpoint not implemented yet")
   ```

## Next Steps

1. **Run tests**: `make test-integration-sqlite`
2. **Check coverage**: `make test-cov`
3. **Add tests**: Follow patterns in existing test files
4. **CI integration**: Add to GitHub Actions/GitLab CI

## Support

For issues or questions:
1. Check this guide
2. Review `tests/integration/README.md`
3. Examine existing test files for patterns
4. Check fixture definitions in `conftest.py`
