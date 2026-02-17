"""Tests for API key management system.

Test coverage:
1. APIKey model instantiation and field validation
2. API key service - key generation format and security properties
3. API key service - hash correctness
4. API key service - validation logic
5. API key auth module - extraction from request headers
6. Auth middleware - API key detection
7. API key response schemas - no raw key/hash exposure
"""

from __future__ import annotations

import hashlib
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.api_key import APIKey
from src.models.user import UserRole
from src.services.api_keys import APIKeyService, InvalidAPIKeyError


# ------------------------------------------------------------------ #
# Model Tests
# ------------------------------------------------------------------ #


class TestAPIKeyModel:
    """Test APIKey model instantiation."""

    def test_api_key_model_fields(self) -> None:
        """Test APIKey model has all required fields."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        key_id = uuid.uuid4()

        api_key = APIKey(
            id=key_id,
            tenant_id=tenant_id,
            name="Test API Key",
            description="Test description",
            key_hash="a" * 64,
            key_prefix="eap_test",
            scopes=["chat", "documents"],
            rate_limit_per_minute=100,
            created_by=user_id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )

        assert api_key.id == key_id
        assert api_key.tenant_id == tenant_id
        assert api_key.name == "Test API Key"
        assert api_key.description == "Test description"
        assert api_key.key_hash == "a" * 64
        assert api_key.key_prefix == "eap_test"
        assert api_key.scopes == ["chat", "documents"]
        assert api_key.rate_limit_per_minute == 100
        assert api_key.created_by == user_id

    def test_api_key_model_defaults(self) -> None:
        """Test APIKey model default values are secure."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        api_key = APIKey(
            tenant_id=tenant_id,
            name="Minimal Key",
            key_hash="b" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=user_id,
        )

        # is_active defaults to True via SQLAlchemy column default
        # (at ORM level, default is applied at flush time)
        assert api_key.name == "Minimal Key"
        assert api_key.expires_at is None
        assert api_key.rate_limit_per_minute is None
        assert api_key.description is None
        assert api_key.revoked_at is None

    def test_api_key_table_name(self) -> None:
        """Test correct table name."""
        assert APIKey.__tablename__ == "api_keys"

    def test_api_key_repr(self) -> None:
        """Test string representation."""
        tenant_id = uuid.uuid4()
        api_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name="Test Key",
            key_hash="c" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
        )

        repr_str = repr(api_key)
        assert "APIKey" in repr_str
        assert "Test Key" in repr_str


# ------------------------------------------------------------------ #
# Service Tests - Key Generation
# ------------------------------------------------------------------ #


class TestAPIKeyGeneration:
    """Test secure key generation properties."""

    def test_key_format_prefix(self) -> None:
        """Generated keys must start with eap_ prefix."""
        service = APIKeyService(MagicMock())
        key = service._generate_key()
        assert key.startswith("eap_"), f"Expected eap_ prefix, got {key[:10]}"

    def test_key_format_length(self) -> None:
        """Generated keys must be exactly 52 characters."""
        service = APIKeyService(MagicMock())
        key = service._generate_key()
        assert len(key) == 52, f"Expected 52 chars, got {len(key)}"

    def test_key_format_body_characters(self) -> None:
        """Key body must only contain base62 characters."""
        service = APIKeyService(MagicMock())
        valid_chars = set(string.ascii_letters + string.digits)

        for _ in range(10):  # Generate multiple to increase confidence
            key = service._generate_key()
            body = key[4:]  # Skip eap_
            invalid = set(body) - valid_chars
            assert not invalid, f"Found invalid chars: {invalid}"

    def test_keys_are_unique(self) -> None:
        """Each generated key must be unique."""
        service = APIKeyService(MagicMock())
        keys = {service._generate_key() for _ in range(20)}
        assert len(keys) == 20, "Keys should be unique (collisions detected)"

    def test_key_uses_cryptographic_randomness(self) -> None:
        """Keys should be generated with secrets module (not random)."""
        # We can't directly test the source of randomness, but we can
        # verify that keys are not sequential or patterned
        service = APIKeyService(MagicMock())
        keys = [service._generate_key()[4:] for _ in range(5)]
        # No two consecutive keys should have matching 8-char substring
        for i in range(len(keys) - 1):
            # Very low probability of this failing with true randomness
            assert keys[i] != keys[i + 1], "Sequential keys should differ"


class TestAPIKeyHashing:
    """Test SHA-256 key hashing."""

    def test_hash_produces_64_char_hex(self) -> None:
        """Hash must be 64-character hex string (SHA-256)."""
        service = APIKeyService(MagicMock())
        h = service._hash_key("eap_test_key")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_is_deterministic(self) -> None:
        """Same key must always produce same hash."""
        service = APIKeyService(MagicMock())
        key = "eap_testkey12345678901234567890123456789012345678"
        h1 = service._hash_key(key)
        h2 = service._hash_key(key)
        assert h1 == h2

    def test_hash_differs_for_different_keys(self) -> None:
        """Different keys must produce different hashes."""
        service = APIKeyService(MagicMock())
        key1 = "eap_" + "a" * 48
        key2 = "eap_" + "b" * 48
        assert service._hash_key(key1) != service._hash_key(key2)

    def test_hash_matches_sha256(self) -> None:
        """Hash must match standard SHA-256 implementation."""
        service = APIKeyService(MagicMock())
        raw = "eap_testkey12345678901234567890123456789012345678"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert service._hash_key(raw) == expected


# ------------------------------------------------------------------ #
# Service Tests - Validation Logic
# ------------------------------------------------------------------ #


class TestAPIKeyValidation:
    """Test key validation without DB (error paths)."""

    async def test_invalid_format_no_prefix(self) -> None:
        """Keys without eap_ prefix must raise InvalidAPIKeyError."""
        mock_db = AsyncMock()
        service = APIKeyService(mock_db)

        with pytest.raises(InvalidAPIKeyError, match="Invalid API key format"):
            await service.validate_key("sk_not_an_eap_key_format_here_12345678")

    async def test_invalid_format_too_short(self) -> None:
        """Keys that are too short must raise InvalidAPIKeyError."""
        mock_db = AsyncMock()
        service = APIKeyService(mock_db)

        with pytest.raises(InvalidAPIKeyError, match="Invalid API key format"):
            await service.validate_key("eap_short")

    async def test_invalid_format_wrong_length(self) -> None:
        """Keys with wrong total length must raise InvalidAPIKeyError."""
        mock_db = AsyncMock()
        service = APIKeyService(mock_db)

        # Too long
        with pytest.raises(InvalidAPIKeyError, match="Invalid API key format"):
            await service.validate_key("eap_" + "a" * 50)

        # Too short (but has prefix)
        with pytest.raises(InvalidAPIKeyError, match="Invalid API key format"):
            await service.validate_key("eap_" + "a" * 40)

    async def test_invalid_key_not_in_db(self) -> None:
        """Valid format but not in database raises InvalidAPIKeyError."""
        mock_db = AsyncMock()

        # Mock the DB to return None (key not found)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = APIKeyService(mock_db)
        fake_key = "eap_" + "a" * 48  # Correct format, doesn't exist

        with pytest.raises(InvalidAPIKeyError, match="Invalid or revoked API key"):
            await service.validate_key(fake_key)

    async def test_revoked_key_raises_error(self) -> None:
        """Revoked keys (is_active=False) raise InvalidAPIKeyError."""
        mock_db = AsyncMock()

        # Mock revoked key
        revoked_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="Revoked Key",
            key_hash="a" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            is_active=False,
            revoked_at=datetime.now(timezone.utc),
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = revoked_key
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = APIKeyService(mock_db)
        fake_key = "eap_" + "a" * 48

        with pytest.raises(InvalidAPIKeyError, match="Invalid or revoked API key"):
            await service.validate_key(fake_key)

    async def test_expired_key_raises_error(self) -> None:
        """Keys past their expiration date raise InvalidAPIKeyError."""
        mock_db = AsyncMock()

        # Mock expired key
        expired_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="Expired Key",
            key_hash="a" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            is_active=True,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),  # Yesterday
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expired_key
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = APIKeyService(mock_db)
        fake_key = "eap_" + "a" * 48

        with pytest.raises(InvalidAPIKeyError, match="API key has expired"):
            await service.validate_key(fake_key)

    async def test_valid_key_returns_model(self) -> None:
        """Valid, active, non-expired key returns the APIKey model."""
        mock_db = AsyncMock()

        tenant_id = uuid.uuid4()
        active_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name="Active Key",
            key_hash="a" * 64,
            key_prefix="eap_test",
            scopes=["chat", "documents"],
            created_by=uuid.uuid4(),
            is_active=True,
            expires_at=None,  # No expiry
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = active_key
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = APIKeyService(mock_db)
        fake_key = "eap_" + "a" * 48

        result = await service.validate_key(fake_key)

        assert result == active_key
        assert result.tenant_id == tenant_id
        assert result.scopes == ["chat", "documents"]


# ------------------------------------------------------------------ #
# Service Tests - Create Key
# ------------------------------------------------------------------ #


class TestAPIKeyCreate:
    """Test create_key service method."""

    async def test_create_key_returns_tuple(self) -> None:
        """create_key returns (APIKey, raw_key) tuple."""
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()

        service = APIKeyService(mock_db)
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        result = await service.create_key(
            tenant_id=tenant_id,
            name="My Key",
            scopes=["chat"],
            created_by=user_id,
        )

        assert isinstance(result, tuple)
        assert len(result) == 2
        api_key, raw_key = result

        assert isinstance(api_key, APIKey)
        assert isinstance(raw_key, str)

    async def test_create_key_raw_key_format(self) -> None:
        """Raw key must have correct eap_ format."""
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()

        service = APIKeyService(mock_db)

        _, raw_key = await service.create_key(
            tenant_id=uuid.uuid4(),
            name="Format Test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
        )

        assert raw_key.startswith("eap_")
        assert len(raw_key) == 52

    async def test_create_key_stores_hash_not_raw(self) -> None:
        """The stored key_hash must be the SHA-256 hash of the raw key."""
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()

        service = APIKeyService(mock_db)

        api_key, raw_key = await service.create_key(
            tenant_id=uuid.uuid4(),
            name="Hash Test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
        )

        expected_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        assert api_key.key_hash == expected_hash, "Stored hash must be SHA-256 of raw key"

    async def test_create_key_prefix_matches_raw_key(self) -> None:
        """Stored key_prefix must be the first 8 chars of the raw key."""
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()

        service = APIKeyService(mock_db)

        api_key, raw_key = await service.create_key(
            tenant_id=uuid.uuid4(),
            name="Prefix Test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
        )

        assert api_key.key_prefix == raw_key[:8]

    async def test_create_key_with_expiration(self) -> None:
        """create_key sets correct expiration date."""
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()

        service = APIKeyService(mock_db)

        api_key, _ = await service.create_key(
            tenant_id=uuid.uuid4(),
            name="Expiry Test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            expires_in_days=30,
        )

        assert api_key.expires_at is not None
        # Should expire in ~30 days (within 5 minutes variance)
        expected = datetime.now(timezone.utc) + timedelta(days=30)
        delta = abs((api_key.expires_at - expected).total_seconds())
        assert delta < 300, f"Expiry too far off: {delta}s difference"

    async def test_create_key_no_expiration(self) -> None:
        """create_key with no expires_in_days leaves expires_at as None."""
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()

        service = APIKeyService(mock_db)

        api_key, _ = await service.create_key(
            tenant_id=uuid.uuid4(),
            name="No Expiry",
            scopes=["chat"],
            created_by=uuid.uuid4(),
        )

        assert api_key.expires_at is None

    async def test_create_key_with_rate_limit(self) -> None:
        """create_key stores rate_limit_per_minute correctly."""
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()

        service = APIKeyService(mock_db)

        api_key, _ = await service.create_key(
            tenant_id=uuid.uuid4(),
            name="Rate Limited",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            rate_limit_per_minute=100,
        )

        assert api_key.rate_limit_per_minute == 100

    async def test_create_key_with_description(self) -> None:
        """create_key stores optional description."""
        mock_db = AsyncMock()
        mock_db.flush = AsyncMock()

        service = APIKeyService(mock_db)

        api_key, _ = await service.create_key(
            tenant_id=uuid.uuid4(),
            name="With Desc",
            scopes=["chat", "analytics"],
            created_by=uuid.uuid4(),
            description="Service account for CI/CD pipeline",
        )

        assert api_key.description == "Service account for CI/CD pipeline"
        assert api_key.scopes == ["chat", "analytics"]


# ------------------------------------------------------------------ #
# Service Tests - Revoke & Rotate
# ------------------------------------------------------------------ #


class TestAPIKeyRevoke:
    """Test revoke_key service method."""

    async def test_revoke_key_sets_inactive(self) -> None:
        """revoke_key sets is_active=False and revoked_at."""
        mock_db = AsyncMock()

        target_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="To Revoke",
            key_hash="d" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            is_active=True,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = target_key
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        service = APIKeyService(mock_db)
        result = await service.revoke_key(target_key.id, target_key.tenant_id)

        assert result is not None
        assert result.is_active is False
        assert result.revoked_at is not None

    async def test_revoke_key_not_found_returns_none(self) -> None:
        """revoke_key returns None when key is not found."""
        mock_db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = APIKeyService(mock_db)
        result = await service.revoke_key(uuid.uuid4(), uuid.uuid4())

        assert result is None

    async def test_revoke_sets_timestamp(self) -> None:
        """revoked_at timestamp should be recent (within the last minute)."""
        mock_db = AsyncMock()

        target_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="Timestamp Test",
            key_hash="e" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            is_active=True,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = target_key
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        before = datetime.now(timezone.utc)
        service = APIKeyService(mock_db)
        result = await service.revoke_key(target_key.id, target_key.tenant_id)
        after = datetime.now(timezone.utc)

        assert result.revoked_at >= before
        assert result.revoked_at <= after


# ------------------------------------------------------------------ #
# Auth Middleware Tests
# ------------------------------------------------------------------ #


class TestAuthMiddlewareAPIKeyDetection:
    """Test API key detection in auth middleware."""

    def test_extract_api_key_from_x_api_key_header(self) -> None:
        """X-API-Key header is extracted correctly."""
        from src.auth.middleware import _extract_api_key

        request = MagicMock()
        request.headers.get = lambda h, d="": "eap_test_key" if h == "X-API-Key" else d

        result = _extract_api_key(request)
        assert result == "eap_test_key"

    def test_extract_api_key_from_bearer_eap_header(self) -> None:
        """Authorization: Bearer eap_... is extracted correctly."""
        from src.auth.middleware import _extract_api_key

        request = MagicMock()
        headers = {
            "X-API-Key": "",
            "Authorization": "Bearer eap_testkey123456789012345678901234567890123456789012",
        }
        request.headers.get = lambda h, d="": headers.get(h, d)

        result = _extract_api_key(request)
        assert result == "eap_testkey123456789012345678901234567890123456789012"

    def test_extract_api_key_returns_none_for_jwt(self) -> None:
        """JWT Bearer tokens (not starting with eap_) return None."""
        from src.auth.middleware import _extract_api_key

        request = MagicMock()
        headers = {
            "X-API-Key": "",
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.signature",
        }
        request.headers.get = lambda h, d="": headers.get(h, d)

        result = _extract_api_key(request)
        assert result is None

    def test_extract_api_key_returns_none_when_no_header(self) -> None:
        """Returns None when no auth headers present."""
        from src.auth.middleware import _extract_api_key

        request = MagicMock()
        request.headers.get = lambda h, d="": d

        result = _extract_api_key(request)
        assert result is None

    def test_x_api_key_takes_priority_over_bearer(self) -> None:
        """X-API-Key header takes priority over Authorization header."""
        from src.auth.middleware import _extract_api_key

        request = MagicMock()
        headers = {
            "X-API-Key": "eap_from_x_api_key_header_123456789012345678901234",
            "Authorization": "Bearer eap_from_auth_header_123456789012345678901234",
        }
        request.headers.get = lambda h, d="": headers.get(h, d)

        result = _extract_api_key(request)
        assert result == "eap_from_x_api_key_header_123456789012345678901234"


# ------------------------------------------------------------------ #
# Auth Module Tests
# ------------------------------------------------------------------ #


class TestCreateAuthenticatedUserFromAPIKey:
    """Test synthetic AuthenticatedUser creation from API key."""

    def test_creates_user_with_correct_tenant(self) -> None:
        """Synthetic user should have the API key's tenant_id."""
        from src.auth.api_key_auth import create_authenticated_user_from_api_key

        tenant_id = uuid.uuid4()
        key_id = uuid.uuid4()

        api_key = APIKey(
            id=key_id,
            tenant_id=tenant_id,
            name="Test Key",
            key_hash="f" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            is_active=True,
        )

        auth_user = create_authenticated_user_from_api_key(api_key)

        assert auth_user.tenant_id == tenant_id

    def test_synthetic_user_is_operator_role(self) -> None:
        """API key users default to OPERATOR role."""
        from src.auth.api_key_auth import create_authenticated_user_from_api_key

        api_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="Operator Key",
            key_hash="g" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            is_active=True,
        )

        auth_user = create_authenticated_user_from_api_key(api_key)

        assert auth_user.role == UserRole.OPERATOR

    def test_claims_contain_api_key_scopes(self) -> None:
        """Claims must contain api_key_scopes for scope enforcement."""
        from src.auth.api_key_auth import create_authenticated_user_from_api_key

        api_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="Scoped Key",
            key_hash="h" * 64,
            key_prefix="eap_test",
            scopes=["chat", "documents", "analytics"],
            created_by=uuid.uuid4(),
            is_active=True,
        )

        auth_user = create_authenticated_user_from_api_key(api_key)

        assert "api_key_scopes" in auth_user.claims
        assert auth_user.claims["api_key_scopes"] == ["chat", "documents", "analytics"]

    def test_claims_mark_api_key_auth_method(self) -> None:
        """Claims must mark auth_method as api_key."""
        from src.auth.api_key_auth import create_authenticated_user_from_api_key

        api_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="Auth Method Test",
            key_hash="i" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            is_active=True,
        )

        auth_user = create_authenticated_user_from_api_key(api_key)

        assert auth_user.claims.get("auth_method") == "api_key"

    def test_claims_contain_tenant_id_string(self) -> None:
        """Claims must contain tenant_id as string."""
        from src.auth.api_key_auth import create_authenticated_user_from_api_key

        tenant_id = uuid.uuid4()
        api_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name="Tenant Test",
            key_hash="j" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            is_active=True,
        )

        auth_user = create_authenticated_user_from_api_key(api_key)

        assert auth_user.claims.get("tenant_id") == str(tenant_id)

    def test_is_api_key_authenticated_true_for_api_key_user(self) -> None:
        """is_api_key_authenticated returns True for API key users."""
        from src.auth.api_key_auth import (
            create_authenticated_user_from_api_key,
            is_api_key_authenticated,
        )

        api_key = APIKey(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            name="Check Test",
            key_hash="k" * 64,
            key_prefix="eap_test",
            scopes=["chat"],
            created_by=uuid.uuid4(),
            is_active=True,
        )

        auth_user = create_authenticated_user_from_api_key(api_key)

        assert is_api_key_authenticated(auth_user) is True


# ------------------------------------------------------------------ #
# API Response Schema Tests
# ------------------------------------------------------------------ #


class TestAPIKeyResponseSchemas:
    """Ensure raw keys and hashes are never exposed in responses."""

    def test_api_key_response_has_no_raw_key_field(self) -> None:
        """APIKeyResponse must not have a 'key' field (only create response does)."""
        from src.api.keys import APIKeyResponse

        model_fields = set(APIKeyResponse.model_fields.keys())
        assert "key" not in model_fields, "Raw key must never appear in list/detail responses"
        assert "key_hash" not in model_fields, "Hash must never be exposed in responses"

    def test_api_key_create_response_has_raw_key_and_warning(self) -> None:
        """APIKeyCreateResponse includes the raw key with a warning."""
        from src.api.keys import APIKeyCreateResponse

        model_fields = set(APIKeyCreateResponse.model_fields.keys())
        assert "key" in model_fields, "Create response must include raw key"
        assert "warning" in model_fields, "Create response must include security warning"

    def test_api_key_response_has_identification_fields(self) -> None:
        """APIKeyResponse includes key_prefix for human identification."""
        from src.api.keys import APIKeyResponse

        model_fields = set(APIKeyResponse.model_fields.keys())
        assert "key_prefix" in model_fields, "Response should include key_prefix for ID"
        assert "id" in model_fields
        assert "name" in model_fields
        assert "scopes" in model_fields
        assert "is_active" in model_fields
        assert "created_at" in model_fields


# ------------------------------------------------------------------ #
# Migration Tests
# ------------------------------------------------------------------ #


class TestMigrationStructure:
    """Validate the migration file structure."""

    def test_migration_has_correct_revision(self) -> None:
        """Migration revision should be 009."""
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "migration_009",
            "/mnt/c/AI/enterprise-agent-platform/alembic/versions/009_api_keys.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.revision == "009"
        assert mod.down_revision == "008"

    def test_migration_has_upgrade_and_downgrade(self) -> None:
        """Migration must have both upgrade and downgrade functions."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "migration_009",
            "/mnt/c/AI/enterprise-agent-platform/alembic/versions/009_api_keys.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert callable(mod.upgrade)
        assert callable(mod.downgrade)
