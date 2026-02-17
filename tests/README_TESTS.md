# Comprehensive Test Suite

This directory contains comprehensive tests for the enterprise agent platform, focusing on compliance, operations, and API modules.

## Test Structure

```
tests/
├── conftest.py                      # Shared fixtures (108 lines)
├── compliance/
│   ├── test_audit_export.py        # SOC 2 evidence tests (240 lines)
│   └── test_gdpr.py                # GDPR rights tests (327 lines)
├── core/
│   └── test_security.py            # Security & validation tests (231 lines)
├── api/
│   ├── test_chat.py                # Chat API tests (192 lines)
│   └── test_plans.py               # Plan API tests (207 lines)
├── infra/
│   └── test_health.py              # Health check tests (175 lines)
└── test_config.py                  # Configuration tests (167 lines)
```

## Test Coverage

### Compliance Tests (567 lines)

#### test_audit_export.py (~240 lines)
- ✓ SOC 2 evidence package generation
- ✓ Audit log filtering by date range
- ✓ Evidence includes required fields (CC6, A1, C1, PI1)
- ✓ Tenant isolation in exports
- ✓ JSON and CSV export formats
- Mock: DB queries for audit logs, users, documents

#### test_gdpr.py (~327 lines)
- ✓ Right to access (Article 15) - complete data export
- ✓ Right to erasure (Article 17) - data anonymization
- ✓ Right to data portability (Article 20) - JSON export
- ✓ PII data collection for export
- ✓ Audit log preservation during erasure
- ✓ 30-day deadline enforcement
- Mock: DB queries for users, conversations, documents, memories

### Core Security Tests (231 lines)

#### test_security.py (~231 lines)
- ✓ Input validation (log injection prevention)
- ✓ PII detection (email, phone, SSN patterns)
- ✓ PII redaction in responses
- ✓ Data classification enforcement (Class I-IV)
- ✓ Content-Type validation
- ✓ Custom PII pattern support

### API Tests (399 lines)

#### test_chat.py (~192 lines)
- ✓ POST /api/v1/chat returns response
- ✓ SSE streaming endpoint
- ✓ Unauthenticated request gets 401
- ✓ Conversation context maintained
- ✓ Rate limiting enforcement
- Use: httpx AsyncClient with TestClient
- Mock: orchestrator and auth

#### test_plans.py (~207 lines)
- ✓ Create plan (OPERATOR role required)
- ✓ Approve plan (transitions to approved)
- ✓ Reject plan (transitions to rejected)
- ✓ List plans filtered by tenant
- ✓ Permission checks (VIEWER denied)
- Mock: auth + DB

### Infrastructure Tests (175 lines)

#### test_health.py (~175 lines)
- ✓ /health/live returns 200
- ✓ /health/ready checks DB and Redis
- ✓ Unhealthy DB returns 503
- ✓ Component-level health status
- ✓ Timeout handling
- Mock: Redis and DB connections

### Configuration Tests (167 lines)

#### test_config.py (~167 lines)
- ✓ Default settings load correctly
- ✓ Production validation rejects default secrets
- ✓ is_dev and is_prod properties
- ✓ Environment enum values
- ✓ Rate limiting configuration
- ✓ Model routing configuration
- ✓ Token budget validation

## Shared Fixtures (conftest.py)

- `test_tenant_id`: Fixed UUID for consistent testing
- `test_user_id`: Fixed user UUID
- `fake_settings`: Test environment configuration
- `mock_db_session`: AsyncMock for database sessions
- `auth_headers`: Valid JWT Bearer token
- `test_user`: Test user with OPERATOR role
- `test_admin_user`: Test admin user
- `mock_audit_service`: Mock audit logging

## Running Tests

```bash
# Run all tests
pytest tests/

# Run specific module
pytest tests/compliance/test_audit_export.py

# Run with coverage
pytest --cov=src --cov-report=html tests/

# Run with verbose output
pytest -v tests/

# Run only specific test
pytest tests/compliance/test_gdpr.py::TestGDPRService::test_process_access_request_returns_all_personal_data
```

## Test Philosophy

All tests follow these principles:

1. **Mock External Dependencies**: Database, Redis, LLM services are mocked
2. **Async Support**: Uses `pytest-asyncio` for async tests
3. **Isolation**: Each test is independent and can run in any order
4. **Realistic**: Tests use realistic data structures and patterns
5. **Coverage**: Focus on critical paths and edge cases

## Dependencies

Required packages (in pyproject.toml):
```toml
[tool.poetry.dev-dependencies]
pytest = "^7.4.0"
pytest-asyncio = "^0.21.0"
pytest-cov = "^4.1.0"
httpx = "^0.24.0"
```

## Notes

- All file paths returned are absolute paths
- Tests use mocking extensively to avoid external dependencies
- Each test module focuses on a specific component or feature
- Test names clearly describe what is being tested
- Mock DB queries use AsyncMock for async database operations
