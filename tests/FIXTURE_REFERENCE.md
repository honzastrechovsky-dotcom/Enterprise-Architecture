# Test Fixtures Quick Reference

## Fixture Dependency Tree

```
TEST_JWT_SECRET (constant)
│
├── make_token() (helper function)
│   └── Used by: auth_headers, client_viewer_a, client_admin_a, client_admin_b
│
├── fake_settings
│   ├── dev_jwt_secret = TEST_JWT_SECRET
│   └── environment = TEST
│       └── test_app (FastAPI app with test settings)
│           ├── client (unauthenticated AsyncClient)
│           ├── client_viewer_a (authenticated as viewer, tenant A)
│           ├── client_admin_a (authenticated as admin, tenant A)
│           └── client_admin_b (authenticated as admin, tenant B)
│
├── tenant_a (UUID string)
│   ├── test_tenant_id (UUID object)
│   ├── viewer_user_a (dict)
│   └── admin_user_a (dict)
│
├── tenant_b (UUID string)
│   └── admin_user_b (dict)
│
├── test_user_id (UUID object)
│
├── mock_db_session (AsyncMock - unit tests)
│
└── db_session (AsyncMock - integration tests, TODO: real DB)
```

## Quick Usage Guide

### Making Authenticated Requests

```python
# Option 1: Use pre-authenticated client
async def test_with_viewer(client_viewer_a):
    response = await client_viewer_a.get("/api/v1/me")
    assert response.status_code == 200

# Option 2: Use base client + auth_headers
async def test_with_headers(client, auth_headers):
    response = await client.get("/api/v1/me", headers=auth_headers)
    assert response.status_code == 200

# Option 3: Make custom token
async def test_custom(client, tenant_a):
    token = make_token(sub="custom-user", tenant_id=tenant_a, role="operator")
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get("/api/v1/me", headers=headers)
    assert response.status_code == 200
```

### Testing Multi-Tenant Isolation

```python
async def test_tenant_isolation(client_admin_a, client_admin_b):
    # Admin A creates resource
    resp = await client_admin_a.post("/api/v1/resources", json={"name": "test"})
    resource_id = resp.json()["id"]

    # Admin B from different tenant should not see it
    resp = await client_admin_b.get(f"/api/v1/resources/{resource_id}")
    assert resp.status_code == 404  # Not found (tenant isolation)
```

### Testing Role-Based Access Control

```python
async def test_rbac(client_viewer_a, client_admin_a):
    # Viewer cannot create users
    resp = await client_viewer_a.post("/api/v1/users", json={...})
    assert resp.status_code == 403  # Forbidden

    # Admin can create users
    resp = await client_admin_a.post("/api/v1/users", json={...})
    assert resp.status_code == 201  # Created
```

### Mocking Database Operations

```python
async def test_repository(mock_db_session):
    # Setup mock return value
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = User(id=uuid4(), ...)
    mock_db_session.execute.return_value = mock_result

    # Test repository method
    repo = UserRepository(mock_db_session)
    user = await repo.get_by_email("test@example.com")

    # Verify
    assert user is not None
    mock_db_session.execute.assert_called_once()
```

## Fixture Cheat Sheet

| Fixture | Type | Purpose | Usage |
|---------|------|---------|-------|
| `TEST_JWT_SECRET` | str | JWT signing key | Used internally by make_token |
| `make_token(...)` | function | Create test JWT | `token = make_token(sub="user", tenant_id="...", role="admin")` |
| `fake_settings` | Settings | Test config | Auto-used by test_app |
| `test_app` | FastAPI | Test app instance | Auto-used by client fixtures |
| `client` | AsyncClient | HTTP client | `await client.get("/api/v1/users")` |
| `client_viewer_a` | AsyncClient | Authenticated viewer | `await client_viewer_a.get(...)` |
| `client_admin_a` | AsyncClient | Authenticated admin (tenant A) | `await client_admin_a.post(...)` |
| `client_admin_b` | AsyncClient | Authenticated admin (tenant B) | `await client_admin_b.get(...)` |
| `tenant_a` | str | Tenant A UUID | `"12345678-1234-5678-1234-567812345678"` |
| `tenant_b` | str | Tenant B UUID | `"87654321-8765-4321-8765-432187654321"` |
| `test_tenant_id` | UUID | Default tenant | `UUID("12345678-1234-5678-1234-567812345678")` |
| `test_user_id` | UUID | Default user | `UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")` |
| `viewer_user_a` | dict | Viewer claims | `{"sub": "...", "tenant_id": tenant_a, "role": "viewer", ...}` |
| `admin_user_a` | dict | Admin claims (A) | `{"sub": "...", "tenant_id": tenant_a, "role": "admin", ...}` |
| `admin_user_b` | dict | Admin claims (B) | `{"sub": "...", "tenant_id": tenant_b, "role": "admin", ...}` |
| `auth_headers` | dict | Auth headers | `{"Authorization": "Bearer ..."}` (viewer_user_a) |
| `mock_db_session` | AsyncMock | Mock DB session | For unit tests, no real DB |
| `db_session` | AsyncMock | DB session | For integration tests (TODO: real DB) |
| `test_user` | User | User model | OPERATOR role user instance |
| `test_admin_user` | User | User model | ADMIN role user instance |
| `test_viewer_user` | User | User model | VIEWER role user instance |
| `mock_audit_service` | AsyncMock | Audit service | `mock.log.assert_called()` |

## Common Patterns

### Pattern 1: Test endpoint requires authentication
```python
async def test_protected_endpoint(client_admin_a):
    response = await client_admin_a.get("/api/v1/protected")
    assert response.status_code == 200
```

### Pattern 2: Test endpoint requires specific role
```python
async def test_admin_only(client_viewer_a, client_admin_a):
    # Viewer forbidden
    resp = await client_viewer_a.delete("/api/v1/users/123")
    assert resp.status_code == 403

    # Admin allowed
    resp = await client_admin_a.delete("/api/v1/users/123")
    assert resp.status_code in (200, 204)
```

### Pattern 3: Test tenant isolation
```python
async def test_isolation(client_admin_a, client_admin_b, tenant_a, tenant_b):
    # Create in tenant A
    resp = await client_admin_a.post("/api/v1/items", json={"name": "A's item"})
    item_id = resp.json()["id"]

    # List in tenant A - should see it
    resp = await client_admin_a.get("/api/v1/items")
    assert any(item["id"] == item_id for item in resp.json()["items"])

    # List in tenant B - should NOT see it
    resp = await client_admin_b.get("/api/v1/items")
    assert not any(item["id"] == item_id for item in resp.json()["items"])
```

### Pattern 4: Test with mocked repository
```python
from unittest.mock import MagicMock

async def test_service_layer(mock_db_session):
    # Mock database responses
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [
        User(id=uuid4(), email="user1@example.com"),
        User(id=uuid4(), email="user2@example.com"),
    ]
    mock_db_session.execute.return_value = mock_result

    # Test service
    service = UserService(mock_db_session)
    users = await service.list_users(tenant_id=uuid4())

    assert len(users) == 2
    mock_db_session.execute.assert_called_once()
```

### Pattern 5: Test with custom user
```python
async def test_custom_user(client, tenant_a):
    # Create user with specific attributes
    token = make_token(
        sub="special-user-123",
        tenant_id=tenant_a,
        role="operator",
        email="operator@example.com"
    )

    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.get("/api/v1/me", headers=headers)

    data = resp.json()
    assert data["email"] == "operator@example.com"
    assert data["role"] == "operator"
```

## Tips

1. **Use pre-authenticated clients** (`client_viewer_a`, `client_admin_a`, `client_admin_b`) for most tests
2. **Use `auth_headers`** if you need just the headers without a client
3. **Use `make_token()`** when you need custom user attributes
4. **Use `mock_db_session`** for unit tests of repositories/services
5. **Use `db_session`** for integration tests (when real DB is set up)
6. **Use `tenant_a` and `tenant_b`** to test multi-tenant isolation
7. **Use User model fixtures** (`test_user`, `test_admin_user`) when you need model instances for testing serialization

## JWT Token Claims

All tokens created by `make_token()` include:

```json
{
  "sub": "<external_id>",
  "tenant_id": "<tenant_uuid>",
  "role": "<admin|operator|viewer>",
  "email": "<user@example.com>",
  "aud": "enterprise-agents-api",
  "iat": <timestamp>,
  "exp": <timestamp + 3600>,
  "jti": "<random_uuid>"
}
```

These match the claims expected by `src/auth/oidc.py` validation.
