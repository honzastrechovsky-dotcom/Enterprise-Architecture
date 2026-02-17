"""Tests for OIDC token validation and discovery.

Coverage:
- Dev token creation and validation
- Prod token validation with JWKS
- Token expiration handling
- Wrong secret rejection
- Missing required claims
- JWKS caching and refresh
- Dev vs prod routing
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from jwt.exceptions import DecodeError, InvalidTokenError

from src.auth.oidc import (
    TokenValidationError,
    _assert_required_claims,
    _fetch_jwks,
    _get_jwks,
    _validate_dev_token,
    create_dev_token,
    validate_token,
)
from src.config import Environment, Settings


TEST_SECRET = "test-secret-for-dev-tokens"
TEST_ISSUER = "http://localhost:8080/realms/test"
# Must match the audience hardcoded in create_dev_token() so validation succeeds.
TEST_AUDIENCE = "enterprise-agents-api"


@pytest.fixture
def dev_settings() -> Settings:
    """Settings configured for dev mode."""
    return Settings(
        environment=Environment.DEV,
        dev_jwt_secret=TEST_SECRET,  # type: ignore[arg-type]
        oidc_issuer_url=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
    )


@pytest.fixture
def prod_settings() -> Settings:
    """Settings configured for production mode."""
    return Settings(
        environment=Environment.PROD,
        secret_key="production-secret-key-123",  # type: ignore[arg-type]
        dev_jwt_secret="production-jwt-secret-456",  # type: ignore[arg-type]
        litellm_api_key="sk-prod-key",  # type: ignore[arg-type]
        oidc_issuer_url=TEST_ISSUER,
        oidc_audience=TEST_AUDIENCE,
    )


class TestCreateDevToken:
    """Test dev token creation."""

    def test_creates_valid_jwt(self) -> None:
        """create_dev_token() generates a valid JWT with all required claims."""
        token = create_dev_token(
            sub="user123",
            tenant_id=str(uuid.uuid4()),
            role="viewer",
            email="test@example.com",
            secret=TEST_SECRET,
            expires_in=3600,
        )

        # Should be a valid JWT
        assert isinstance(token, str)
        assert token.count(".") == 2  # header.payload.signature

        # Should decode successfully (skip aud verification - we just inspect claims)
        decoded = jwt.decode(
            token, TEST_SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert decoded["sub"] == "user123"
        assert decoded["role"] == "viewer"
        assert decoded["email"] == "test@example.com"
        assert "tenant_id" in decoded
        assert "exp" in decoded
        assert "iat" in decoded
        assert "jti" in decoded

    def test_respects_expiration(self) -> None:
        """Token expiration is set correctly."""
        token = create_dev_token(
            sub="user123",
            tenant_id=str(uuid.uuid4()),
            role="admin",
            secret=TEST_SECRET,
            expires_in=7200,
        )

        decoded = jwt.decode(
            token, TEST_SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        exp = decoded["exp"]
        iat = decoded["iat"]

        # Expiration should be ~7200 seconds after issued
        assert (exp - iat) == 7200

    def test_unique_jti_per_token(self) -> None:
        """Each token gets a unique jti (JWT ID)."""
        token1 = create_dev_token(
            sub="user123",
            tenant_id=str(uuid.uuid4()),
            secret=TEST_SECRET,
        )
        token2 = create_dev_token(
            sub="user123",
            tenant_id=str(uuid.uuid4()),
            secret=TEST_SECRET,
        )

        decoded1 = jwt.decode(
            token1, TEST_SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        decoded2 = jwt.decode(
            token2, TEST_SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )

        assert decoded1["jti"] != decoded2["jti"]


class TestValidateDevToken:
    """Test dev token validation (ENVIRONMENT=dev)."""

    def test_validates_valid_token(self, dev_settings: Settings) -> None:
        """Valid dev token passes validation."""
        token = create_dev_token(
            sub="user456",
            tenant_id=str(uuid.uuid4()),
            role="operator",
            email="dev@example.com",
            secret=TEST_SECRET,
            expires_in=3600,
        )

        claims = _validate_dev_token(token, dev_settings)

        assert claims["sub"] == "user456"
        assert claims["role"] == "operator"
        assert claims["email"] == "dev@example.com"

    def test_rejects_expired_token(self, dev_settings: Settings) -> None:
        """Expired dev token raises TokenValidationError."""
        # Create token that expired 1 hour ago
        token = create_dev_token(
            sub="user789",
            tenant_id=str(uuid.uuid4()),
            secret=TEST_SECRET,
            expires_in=-3600,  # Negative = already expired
        )

        with pytest.raises(TokenValidationError, match="Dev token validation failed"):
            _validate_dev_token(token, dev_settings)

    def test_rejects_wrong_secret(self, dev_settings: Settings) -> None:
        """Token signed with wrong secret raises TokenValidationError."""
        token = create_dev_token(
            sub="user999",
            tenant_id=str(uuid.uuid4()),
            secret="WRONG_SECRET",
            expires_in=3600,
        )

        with pytest.raises(TokenValidationError, match="Dev token validation failed"):
            _validate_dev_token(token, dev_settings)

    def test_rejects_missing_sub(self, dev_settings: Settings) -> None:
        """Token missing 'sub' claim raises TokenValidationError."""
        # Manually create token without sub - must include aud to pass audience check
        payload = {
            "tenant_id": str(uuid.uuid4()),
            "role": "viewer",
            "aud": TEST_AUDIENCE,
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")

        with pytest.raises(TokenValidationError, match="Missing required JWT claims"):
            _validate_dev_token(token, dev_settings)

    def test_rejects_missing_tenant_id(self, dev_settings: Settings) -> None:
        """Token missing 'tenant_id' claim raises TokenValidationError."""
        payload = {
            "sub": "user123",
            "role": "viewer",
            "aud": TEST_AUDIENCE,
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")

        with pytest.raises(TokenValidationError, match="Missing required JWT claims"):
            _validate_dev_token(token, dev_settings)


class TestAssertRequiredClaims:
    """Test required claims validation."""

    def test_accepts_valid_claims(self) -> None:
        """Claims with all required fields pass."""
        claims = {
            "sub": "user123",
            "tenant_id": str(uuid.uuid4()),
            "role": "admin",
        }
        # Should not raise
        _assert_required_claims(claims)

    def test_rejects_missing_sub(self) -> None:
        """Missing 'sub' raises TokenValidationError."""
        claims = {
            "tenant_id": str(uuid.uuid4()),
            "role": "viewer",
        }
        with pytest.raises(TokenValidationError, match="Missing required JWT claims.*sub"):
            _assert_required_claims(claims)

    def test_rejects_missing_tenant_id(self) -> None:
        """Missing 'tenant_id' raises TokenValidationError."""
        claims = {
            "sub": "user123",
            "role": "viewer",
        }
        with pytest.raises(TokenValidationError, match="Missing required JWT claims.*tenant_id"):
            _assert_required_claims(claims)

    def test_rejects_empty_sub(self) -> None:
        """Empty string 'sub' is treated as missing."""
        claims = {
            "sub": "",
            "tenant_id": str(uuid.uuid4()),
        }
        with pytest.raises(TokenValidationError):
            _assert_required_claims(claims)


class TestValidateTokenRouting:
    """Test that validate_token routes to dev vs prod correctly."""

    @pytest.mark.asyncio
    async def test_routes_to_dev_when_is_dev(self, dev_settings: Settings) -> None:
        """validate_token uses dev validation when settings.is_dev is True."""
        token = create_dev_token(
            sub="user123",
            tenant_id=str(uuid.uuid4()),
            secret=TEST_SECRET,
            expires_in=3600,
        )

        claims = await validate_token(token, dev_settings)

        assert claims["sub"] == "user123"
        assert "tenant_id" in claims

    @pytest.mark.asyncio
    async def test_routes_to_prod_when_not_dev(self, prod_settings: Settings) -> None:
        """validate_token uses JWKS validation when not in dev mode."""
        # Mock JWKS fetch to avoid real network calls
        mock_jwks = {
            "test-key-id": {
                "kty": "RSA",
                "kid": "test-key-id",
                "use": "sig",
                "n": "test-n",
                "e": "AQAB",
            }
        }

        with patch("src.auth.oidc._get_jwks", new_callable=AsyncMock) as mock_get_jwks:
            mock_get_jwks.return_value = mock_jwks

            # Create a token (will fail signature check, but routing is what we test)
            token = create_dev_token(
                sub="user123",
                tenant_id=str(uuid.uuid4()),
                secret=TEST_SECRET,
            )

            # Should attempt JWKS validation (and fail, but that proves routing)
            with pytest.raises(TokenValidationError):
                await validate_token(token, prod_settings)

            # Verify JWKS was called (proves prod path)
            mock_get_jwks.assert_called_once()


class TestJWKSFetching:
    """Test JWKS discovery and caching."""

    @pytest.mark.asyncio
    async def test_fetch_jwks_calls_discovery_endpoint(self) -> None:
        """_fetch_jwks fetches discovery doc and then JWKS."""
        discovery_response = {
            "jwks_uri": "http://localhost:8080/jwks",
            "issuer": TEST_ISSUER,
        }
        jwks_response = {
            "keys": [
                {"kid": "key1", "kty": "RSA", "use": "sig"},
                {"kid": "key2", "kty": "RSA", "use": "sig"},
            ]
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock discovery endpoint - MagicMock so .json() returns value directly
            mock_discovery_resp = MagicMock()
            mock_discovery_resp.json.return_value = discovery_response
            mock_discovery_resp.raise_for_status = MagicMock()

            # Mock JWKS endpoint - MagicMock so .json() returns value directly
            mock_jwks_resp = MagicMock()
            mock_jwks_resp.json.return_value = jwks_response
            mock_jwks_resp.raise_for_status = MagicMock()

            mock_client.get.side_effect = [mock_discovery_resp, mock_jwks_resp]

            result = await _fetch_jwks(TEST_ISSUER)

            # Should have called discovery and JWKS endpoints
            assert mock_client.get.call_count == 2
            assert result == jwks_response

    @pytest.mark.asyncio
    async def test_get_jwks_caches_result(self, prod_settings: Settings) -> None:
        """_get_jwks caches JWKS and reuses for TTL period."""
        mock_jwks_data = {
            "keys": [{"kid": "key1", "kty": "RSA"}]
        }

        # Clear cache
        import src.auth.oidc as oidc_module
        oidc_module._jwks_cache = {}
        oidc_module._jwks_fetched_at = 0.0

        with patch("src.auth.oidc._fetch_jwks", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_jwks_data

            # First call: should fetch
            result1 = await _get_jwks(prod_settings)
            assert mock_fetch.call_count == 1
            assert "key1" in result1

            # Second call immediately: should use cache
            result2 = await _get_jwks(prod_settings)
            assert mock_fetch.call_count == 1  # Still 1 (cached)
            assert result2 == result1

    @pytest.mark.asyncio
    async def test_get_jwks_refreshes_after_ttl(self, prod_settings: Settings) -> None:
        """_get_jwks refreshes JWKS after TTL expires."""
        mock_jwks_data = {
            "keys": [{"kid": "key1", "kty": "RSA"}]
        }

        # Clear cache and set old timestamp
        import src.auth.oidc as oidc_module
        oidc_module._jwks_cache = {"old-key": {"kid": "old-key"}}
        oidc_module._jwks_fetched_at = time.monotonic() - 400  # 400 seconds ago (> TTL)

        with patch("src.auth.oidc._fetch_jwks", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_jwks_data

            result = await _get_jwks(prod_settings)

            # Should have fetched fresh JWKS
            mock_fetch.assert_called_once()
            assert "key1" in result
            assert "old-key" not in result


class TestProdTokenValidation:
    """Test production token validation with JWKS."""

    @pytest.mark.asyncio
    async def test_validates_token_with_correct_kid(self, prod_settings: Settings) -> None:
        """Prod validation succeeds with matching kid in JWKS."""
        # Create a real RS256 token for testing
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        # Generate RSA key pair
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )
        public_key = private_key.public_key()

        # Create token
        payload = {
            "sub": "user123",
            "tenant_id": str(uuid.uuid4()),
            "role": "admin",
            "aud": TEST_AUDIENCE,
            "iss": TEST_ISSUER,
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            "iat": int(datetime.now(timezone.utc).timestamp()),
        }
        token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-kid"})

        # Mock JWKS to return our public key
        mock_jwks = {
            "test-kid": {
                "kty": "RSA",
                "kid": "test-kid",
                "use": "sig",
                "n": "mock-n",
                "e": "AQAB",
            }
        }

        with patch("src.auth.oidc._get_jwks", new_callable=AsyncMock) as mock_get_jwks:
            mock_get_jwks.return_value = mock_jwks

            # Mock PyJWK to use our real public key
            with patch("jwt.PyJWK") as mock_pyjwk_class:
                mock_pyjwk = MagicMock()
                mock_pyjwk.key = public_key
                mock_pyjwk_class.return_value = mock_pyjwk

                claims = await validate_token(token, prod_settings)

                assert claims["sub"] == "user123"
                assert "tenant_id" in claims

    @pytest.mark.asyncio
    async def test_rejects_token_with_wrong_audience(self, prod_settings: Settings) -> None:
        """Token with wrong audience is rejected."""
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )

        payload = {
            "sub": "user123",
            "tenant_id": str(uuid.uuid4()),
            "aud": "WRONG_AUDIENCE",  # Wrong!
            "iss": TEST_ISSUER,
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            "iat": int(datetime.now(timezone.utc).timestamp()),
        }
        token = jwt.encode(payload, private_key, algorithm="RS256")

        mock_jwks = {"key1": {"kid": "key1", "kty": "RSA"}}

        with patch("src.auth.oidc._get_jwks", new_callable=AsyncMock) as mock_get_jwks:
            mock_get_jwks.return_value = mock_jwks

            with patch("jwt.PyJWK") as mock_pyjwk_class:
                mock_pyjwk = MagicMock()
                mock_pyjwk.key = private_key.public_key()
                mock_pyjwk_class.return_value = mock_pyjwk

                with pytest.raises(TokenValidationError, match="Token validation failed"):
                    await validate_token(token, prod_settings)

    @pytest.mark.asyncio
    async def test_handles_token_without_kid(self, prod_settings: Settings) -> None:
        """Token without kid falls back to first available JWKS key."""
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )

        payload = {
            "sub": "user123",
            "tenant_id": str(uuid.uuid4()),
            "role": "admin",
            "aud": TEST_AUDIENCE,
            "iss": TEST_ISSUER,
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            "iat": int(datetime.now(timezone.utc).timestamp()),
        }
        # No kid in header
        token = jwt.encode(payload, private_key, algorithm="RS256")

        mock_jwks = {"fallback-key": {"kid": "fallback-key", "kty": "RSA"}}

        with patch("src.auth.oidc._get_jwks", new_callable=AsyncMock) as mock_get_jwks:
            mock_get_jwks.return_value = mock_jwks

            with patch("jwt.PyJWK") as mock_pyjwk_class:
                mock_pyjwk = MagicMock()
                mock_pyjwk.key = private_key.public_key()
                mock_pyjwk_class.return_value = mock_pyjwk

                claims = await validate_token(token, prod_settings)
                assert claims["sub"] == "user123"

    @pytest.mark.asyncio
    async def test_raises_when_no_jwks_available(self, prod_settings: Settings) -> None:
        """Empty JWKS raises TokenValidationError."""
        token = create_dev_token(
            sub="user123",
            tenant_id=str(uuid.uuid4()),
            secret=TEST_SECRET,
        )

        with patch("src.auth.oidc._get_jwks", new_callable=AsyncMock) as mock_get_jwks:
            mock_get_jwks.return_value = {}  # Empty JWKS

            with pytest.raises(TokenValidationError, match="No JWKS keys available"):
                await validate_token(token, prod_settings)
