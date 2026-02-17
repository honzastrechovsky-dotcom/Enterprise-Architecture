# Auth Module Tests

Comprehensive test suite for authentication and authorization modules.

## Overview

This directory contains test files covering:
- **test_oidc.py** (~250 lines) - OIDC token validation, JWKS discovery, dev vs prod routing
- **test_dependencies.py** (~280 lines) - FastAPI dependencies, JIT provisioning, role checks
- **test_middleware.py** (~170 lines) - Middleware behavior, public path skipping

Total: **~700 lines** of comprehensive test coverage

## Test Files

### test_oidc.py

Tests for OIDC discovery and token validation (`src/auth/oidc.py`).

**Coverage:**
- `create_dev_token()` generates valid JWT tokens
- `validate_token()` with valid dev tokens
- `validate_token()` rejects expired tokens with `TokenValidationError`
- `validate_token()` rejects wrong secret
- Missing required claims (`sub`, `tenant_id`) raise errors
- `_validate_dev_token()` vs `_validate_prod_token()` routing logic
- JWKS fetching from OIDC discovery endpoint
- JWKS caching with 5-minute TTL
- JWKS refresh after TTL expiration
- Production token validation with RS256 signatures
- Token validation with kid matching
- Fallback to first key when kid is missing
- Wrong audience rejection
- Empty JWKS error handling

**Test Classes:**
- `TestCreateDevToken` - Dev token creation
- `TestValidateDevToken` - Dev mode validation
- `TestAssertRequiredClaims` - Required claims validation
- `TestValidateTokenRouting` - Dev vs prod routing
- `TestJWKSFetching` - JWKS discovery and caching
- `TestProdTokenValidation` - Production RS256 validation

**Key Techniques:**
- Mock `httpx.AsyncClient` for JWKS endpoint calls
- Generate real RSA key pairs for RS256 testing
- Mock `_get_jwks()` to avoid network calls
- Time manipulation for TTL testing

### test_dependencies.py

Tests for FastAPI authentication dependencies (`src/auth/dependencies.py`).

**Coverage:**
- `get_current_user()` returns `AuthenticatedUser` for valid claims
- JIT user provisioning: new users auto-created as `VIEWER` role
- JIT provisioning **ignores** elevated role claims (security feature)
- Deactivated user accounts return 403
- Missing `tenant_id` claim returns 401
- Invalid `tenant_id` format (not UUID) returns 401
- `require_role()` allows users with correct role
- `require_role()` denies users with wrong role (403)
- `last_login_at` timestamp updated on authentication
- Token extraction from middleware state vs fallback to header
- AuthenticatedUser wrapper exposes user properties and claims

**Test Classes:**
- `TestExtractAndValidateToken` - Token extraction logic
- `TestGetCurrentUser` - User lookup and JIT provisioning
- `TestRequireRole` - Role-based access control
- `TestAuthenticatedUser` - Wrapper class properties

**Key Techniques:**
- Mock `AsyncSession` with `AsyncMock`
- Capture added users with side effects
- Mock `validate_token` to avoid OIDC calls
- Test datetime updates within tolerance

### test_middleware.py

Tests for authentication middleware (`src/auth/middleware.py`).

**Coverage:**
- `AuthMiddleware` validates valid tokens and sets `request.state.auth_claims`
- Public paths (`/health`, `/docs`, `/openapi.json`) skip validation
- Missing Authorization header sets `auth_claims=None`
- Invalid tokens set `auth_claims=None` (graceful degradation)
- Expired tokens handled gracefully
- Malformed header formats ignored
- Middleware logs successful validation (debug level)
- Middleware logs validation failures (warning level)
- Multiple requests with different tokens handled correctly
- Middleware **does not block** requests (lets dependencies handle auth)

**Test Classes:**
- `TestAuthMiddleware` - Core middleware behavior
- `TestPublicPathSkipping` - Public endpoint bypass
- `TestMiddlewareLogging` - Logging behavior
- `TestMiddlewareIntegration` - End-to-end request flow

**Key Techniques:**
- Use `Starlette` test app with real middleware
- `ASGITransport` for httpx testing
- Mock `get_settings()` in middleware context
- Mock structlog for logging verification

## Running Tests

### Install Dependencies

```bash
# Install all dependencies including dev
pip install -e ".[dev]"

# Or with uv
uv pip install -e ".[dev]"
```

### Run All Auth Tests

```bash
pytest tests/auth/ -v
```

### Run Specific Test File

```bash
pytest tests/auth/test_oidc.py -v
pytest tests/auth/test_dependencies.py -v
pytest tests/auth/test_middleware.py -v
```

### Run Specific Test Class

```bash
pytest tests/auth/test_oidc.py::TestCreateDevToken -v
pytest tests/auth/test_dependencies.py::TestJITProvisioning -v
```

### Run with Coverage

```bash
pytest tests/auth/ --cov=src/auth --cov-report=term-missing
```

## Test Design Principles

### 1. No Real Dependencies

- **No real database** - Use mocked `AsyncSession`
- **No real Redis** - Not needed for auth tests
- **No real OIDC provider** - Mock JWKS endpoints
- **No real HTTP calls** - Mock `httpx.AsyncClient`

### 2. Fast Execution

All tests use mocks, so they run in milliseconds. No network I/O, no database I/O.

### 3. Self-Contained

Each test is independent:
- No shared state between tests
- Fixtures create fresh objects
- Mocks are reset per test

### 4. Realistic Scenarios

Tests use real JWT encoding/decoding:
- Real RSA key pairs for RS256 testing
- Real symmetric HS256 for dev tokens
- Real PyJWT library validation

### 5. Security-Focused

Tests verify security properties:
- JIT provisioning ignores elevated role claims
- Deactivated users cannot authenticate
- Wrong signatures rejected
- Expired tokens rejected
- Missing tenant_id blocked

## Common Patterns

### Mocking AsyncSession

```python
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import AsyncSession

mock_db = AsyncMock(spec=AsyncSession)
mock_result = AsyncMock()
mock_result.scalar_one_or_none.return_value = user
mock_db.execute.return_value = mock_result
```

### Capturing Added Objects

```python
added_user = None

def capture_add(user: User) -> None:
    nonlocal added_user
    added_user = user
    user.id = uuid.uuid4()

mock_db.add.side_effect = capture_add
```

### Mocking JWKS Fetch

```python
with patch("src.auth.oidc._get_jwks", new_callable=AsyncMock) as mock_get_jwks:
    mock_get_jwks.return_value = {"key1": {...}}
    # ... test code
```

### Testing HTTPException

```python
with pytest.raises(HTTPException) as exc_info:
    await get_current_user(invalid_claims, mock_db)

assert exc_info.value.status_code == 401
assert "error message" in exc_info.value.detail
```

## Fixtures

Tests use fixtures from `tests/conftest.py`:
- `test_engine` - Session-scoped database engine
- `db_session` - Per-test database session (rolled back)
- `test_app` - FastAPI app with test DB injected
- `client` - Unauthenticated HTTP client
- `tenant_a`, `tenant_b` - Test tenants
- `admin_user_a`, `viewer_user_a` - Test users
- `make_token()` - Helper to create dev JWTs
- `auth_headers()` - Helper to create auth headers

## Security Notes

### JIT Provisioning Security

The tests verify that JIT (Just-In-Time) user provisioning **always** creates users as `VIEWER` role, even if the JWT claims contain `"role": "admin"`. This prevents privilege escalation via JWT manipulation.

**Test:** `test_jit_ignores_elevated_role_claims`

### Deactivated User Blocking

Tests verify that deactivated users (`is_active=False`) cannot authenticate, returning 403 Forbidden.

**Test:** `test_raises_403_for_deactivated_user`

### Tenant Isolation

Tests verify that `tenant_id` is required in JWT claims and must be a valid UUID. This ensures tenant isolation.

**Tests:**
- `test_raises_401_when_tenant_id_missing`
- `test_raises_401_when_tenant_id_invalid_format`

### Error Message Opacity

Tests verify that role requirement errors don't reveal specific role names, preventing information disclosure.

**Test:** `test_does_not_reveal_role_requirements_in_error`

## Maintenance

### Adding New Tests

When adding new auth features:

1. Add test to appropriate file:
   - OIDC/token logic → `test_oidc.py`
   - Dependencies/user lookup → `test_dependencies.py`
   - Middleware/request flow → `test_middleware.py`

2. Follow existing patterns:
   - Use `@pytest.mark.asyncio` for async tests
   - Mock external dependencies
   - Use descriptive test names
   - Add docstrings

3. Run tests locally before committing:
   ```bash
   pytest tests/auth/ -v
   ```

### Updating Dependencies

If auth module signatures change:

1. Update test mocks to match
2. Update test assertions
3. Keep test coverage at 100%

## Coverage Goals

- **Line coverage:** >95%
- **Branch coverage:** >90%
- **Security scenarios:** 100%

Run coverage report:

```bash
pytest tests/auth/ --cov=src/auth --cov-report=html
open htmlcov/index.html
```
