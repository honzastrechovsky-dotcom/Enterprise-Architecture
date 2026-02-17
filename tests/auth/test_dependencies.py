"""Tests for FastAPI authentication dependencies.

Coverage:
- get_current_user with valid claims returns AuthenticatedUser
- JIT user provisioning: new user auto-created as VIEWER
- JIT provisioning ignores elevated role claims (security)
- Deactivated user returns 403
- Missing tenant_id returns 401
- Invalid tenant_id format returns 401
- require_role() allows correct role
- require_role() denies wrong role with 403
- last_login_at timestamp update
- Token extraction from middleware state vs fallback
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import (
    AuthenticatedUser,
    _extract_and_validate_token,
    get_current_user,
    require_role,
)
from src.auth.oidc import create_dev_token
from src.config import Settings
from src.models.user import User, UserRole


TEST_SECRET = "test-secret-for-deps"


@pytest.fixture
def mock_settings() -> Settings:
    """Mock settings for tests."""
    return Settings(
        environment="dev",  # type: ignore[arg-type]
        dev_jwt_secret=TEST_SECRET,  # type: ignore[arg-type]
    )


@pytest.fixture
def mock_request() -> MagicMock:
    """Mock Starlette Request object."""
    request = MagicMock()
    request.state = MagicMock()
    request.state.auth_claims = None
    request.headers = {}
    return request


@pytest.fixture
def tenant_id() -> uuid.UUID:
    """Test tenant ID."""
    return uuid.uuid4()


@pytest.fixture
def valid_claims(tenant_id: uuid.UUID) -> dict:
    """Valid JWT claims."""
    return {
        "sub": "external-user-123",
        "tenant_id": str(tenant_id),
        "role": "viewer",
        "email": "user@example.com",
        "name": "Test User",
    }


class TestExtractAndValidateToken:
    """Test token extraction from request."""

    @pytest.mark.asyncio
    async def test_uses_middleware_claims_when_available(
        self,
        mock_request: MagicMock,
        mock_settings: Settings,
        valid_claims: dict,
    ) -> None:
        """Uses claims from middleware state if available."""
        mock_request.state.auth_claims = valid_claims

        claims = await _extract_and_validate_token(mock_request, mock_settings)

        assert claims == valid_claims

    @pytest.mark.asyncio
    async def test_extracts_from_header_when_no_middleware_state(
        self,
        mock_request: MagicMock,
        mock_settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        """Falls back to header extraction when middleware didn't run."""
        mock_request.state.auth_claims = None
        token = create_dev_token(
            sub="user123",
            tenant_id=str(tenant_id),
            secret=TEST_SECRET,
            expires_in=3600,
        )
        mock_request.headers = {"Authorization": f"Bearer {token}"}

        with patch("src.auth.dependencies.validate_token", new_callable=AsyncMock) as mock_validate:
            mock_validate.return_value = valid_claims

            claims = await _extract_and_validate_token(mock_request, mock_settings)

            assert claims == valid_claims
            mock_validate.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_401_when_no_authorization_header(
        self,
        mock_request: MagicMock,
        mock_settings: Settings,
    ) -> None:
        """Raises 401 when Authorization header is missing."""
        mock_request.state.auth_claims = None
        mock_request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            await _extract_and_validate_token(mock_request, mock_settings)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert "Missing or invalid" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_401_when_header_missing_bearer_prefix(
        self,
        mock_request: MagicMock,
        mock_settings: Settings,
    ) -> None:
        """Raises 401 when Authorization header doesn't start with 'Bearer '."""
        mock_request.state.auth_claims = None
        mock_request.headers = {"Authorization": "NotBearer sometoken"}

        with pytest.raises(HTTPException) as exc_info:
            await _extract_and_validate_token(mock_request, mock_settings)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_raises_401_on_token_validation_error(
        self,
        mock_request: MagicMock,
        mock_settings: Settings,
    ) -> None:
        """Raises 401 when token validation fails."""
        from src.auth.oidc import TokenValidationError

        mock_request.state.auth_claims = None
        mock_request.headers = {"Authorization": "Bearer invalid-token"}

        with patch("src.auth.dependencies.validate_token", new_callable=AsyncMock) as mock_validate:
            mock_validate.side_effect = TokenValidationError("Token expired")

            with pytest.raises(HTTPException) as exc_info:
                await _extract_and_validate_token(mock_request, mock_settings)

            assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
            assert "Invalid or expired" in exc_info.value.detail


def _make_request_with_claims(claims: dict | None) -> MagicMock:
    """Create a mock Request object with claims set in state."""
    request = MagicMock()
    request.state = MagicMock()
    request.state.auth_claims = claims
    request.state.api_key_raw = None
    request.headers = {}
    return request


class TestGetCurrentUser:
    """Test get_current_user dependency."""

    @pytest.mark.asyncio
    async def test_returns_authenticated_user_for_existing_user(
        self,
        valid_claims: dict,
        tenant_id: uuid.UUID,
    ) -> None:
        """Returns AuthenticatedUser for existing user."""
        # Create mock existing user
        existing_user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            external_id="external-user-123",
            email="user@example.com",
            role=UserRole.VIEWER,
            is_active=True,
        )

        # Mock database session
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        mock_request = _make_request_with_claims(valid_claims)
        authenticated_user = await get_current_user(mock_request, mock_db)

        assert isinstance(authenticated_user, AuthenticatedUser)
        assert authenticated_user.user == existing_user
        assert authenticated_user.claims == valid_claims
        assert authenticated_user.id == existing_user.id
        assert authenticated_user.tenant_id == tenant_id
        assert authenticated_user.role == UserRole.VIEWER

    @pytest.mark.asyncio
    async def test_jit_provisions_new_user_as_viewer(
        self,
        valid_claims: dict,
        tenant_id: uuid.UUID,
    ) -> None:
        """JIT provisioning creates new user as VIEWER role."""
        # Mock database session - no existing user
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # No existing user
        mock_db.execute.return_value = mock_result

        # Track the user that gets added
        added_user = None

        def capture_add(user: User) -> None:
            nonlocal added_user
            added_user = user
            user.id = uuid.uuid4()  # Simulate DB generating ID on flush
            user.is_active = True  # Simulate DB default

        mock_db.add.side_effect = capture_add

        mock_request = _make_request_with_claims(valid_claims)
        authenticated_user = await get_current_user(mock_request, mock_db)

        # Should have created new user
        assert added_user is not None
        assert added_user.external_id == "external-user-123"
        assert added_user.email == "user@example.com"
        assert added_user.display_name == "Test User"
        assert added_user.role == UserRole.VIEWER  # Always VIEWER for JIT
        assert added_user.tenant_id == tenant_id

        # Should have flushed to get ID
        mock_db.flush.assert_called_once()

        # Returned user should be the new one
        assert authenticated_user.user == added_user

    @pytest.mark.asyncio
    async def test_jit_ignores_elevated_role_claims(
        self,
        tenant_id: uuid.UUID,
    ) -> None:
        """JIT provisioning ignores 'admin' role claim for security."""
        # Claims say admin, but JIT should ignore
        claims_with_admin = {
            "sub": "hacker-123",
            "tenant_id": str(tenant_id),
            "role": "admin",  # Trying to escalate!
            "email": "hacker@example.com",
        }

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        added_user = None

        def capture_add(user: User) -> None:
            nonlocal added_user
            added_user = user
            user.id = uuid.uuid4()
            user.is_active = True  # Simulate DB default

        mock_db.add.side_effect = capture_add

        mock_request = _make_request_with_claims(claims_with_admin)
        authenticated_user = await get_current_user(mock_request, mock_db)

        # Role should be VIEWER, not admin
        assert added_user is not None
        assert added_user.role == UserRole.VIEWER
        assert authenticated_user.role == UserRole.VIEWER

    @pytest.mark.asyncio
    async def test_jit_handles_missing_email_claim(
        self,
        tenant_id: uuid.UUID,
    ) -> None:
        """JIT provisioning handles missing email gracefully."""
        claims_no_email = {
            "sub": "user-no-email",
            "tenant_id": str(tenant_id),
            "role": "viewer",
        }

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        added_user = None

        def capture_add(user: User) -> None:
            nonlocal added_user
            added_user = user
            user.id = uuid.uuid4()
            user.is_active = True  # Simulate DB default

        mock_db.add.side_effect = capture_add

        mock_request = _make_request_with_claims(claims_no_email)
        authenticated_user = await get_current_user(mock_request, mock_db)

        # Email should be generated from sub
        assert added_user is not None
        assert added_user.email == "user-no-email@unknown"

    @pytest.mark.asyncio
    async def test_raises_403_for_deactivated_user(
        self,
        valid_claims: dict,
        tenant_id: uuid.UUID,
    ) -> None:
        """Returns 403 when user account is deactivated."""
        deactivated_user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            external_id="external-user-123",
            email="user@example.com",
            role=UserRole.VIEWER,
            is_active=False,  # Deactivated!
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = deactivated_user
        mock_db.execute.return_value = mock_result

        mock_request = _make_request_with_claims(valid_claims)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(mock_request, mock_db)

        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert "deactivated" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_401_when_tenant_id_missing(
        self,
    ) -> None:
        """Returns 401 when tenant_id claim is missing."""
        claims_no_tenant = {
            "sub": "user123",
            "role": "viewer",
            # tenant_id missing!
        }

        mock_db = AsyncMock(spec=AsyncSession)

        mock_request = _make_request_with_claims(claims_no_tenant)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(mock_request, mock_db)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert "tenant_id" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_401_when_tenant_id_invalid_format(
        self,
    ) -> None:
        """Returns 401 when tenant_id is not a valid UUID."""
        claims_bad_tenant = {
            "sub": "user123",
            "tenant_id": "not-a-uuid",
            "role": "viewer",
        }

        mock_db = AsyncMock(spec=AsyncSession)

        mock_request = _make_request_with_claims(claims_bad_tenant)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(mock_request, mock_db)

        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert "tenant_id" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_updates_last_login_at_timestamp(
        self,
        valid_claims: dict,
        tenant_id: uuid.UUID,
    ) -> None:
        """Updates last_login_at timestamp on successful auth."""
        existing_user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            external_id="external-user-123",
            email="user@example.com",
            role=UserRole.VIEWER,
            is_active=True,
            last_login_at=None,
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        before = datetime.now(timezone.utc)
        mock_request = _make_request_with_claims(valid_claims)
        authenticated_user = await get_current_user(mock_request, mock_db)
        after = datetime.now(timezone.utc)

        # last_login_at should be updated
        assert existing_user.last_login_at is not None
        assert before <= existing_user.last_login_at <= after


class TestRequireRole:
    """Test require_role dependency factory."""

    @pytest.mark.asyncio
    async def test_allows_user_with_correct_role(self) -> None:
        """User with required role passes check."""
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            external_id="user123",
            email="admin@test.com",
            role=UserRole.ADMIN,
            is_active=True,
        )
        authenticated_user = AuthenticatedUser(user=user, claims={})

        # Create dependency checker
        check_role = require_role(UserRole.ADMIN)

        # Should not raise
        result = await check_role(authenticated_user)
        assert result == authenticated_user

    @pytest.mark.asyncio
    async def test_allows_user_with_one_of_allowed_roles(self) -> None:
        """User with any of the allowed roles passes."""
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            external_id="user123",
            email="operator@test.com",
            role=UserRole.OPERATOR,
            is_active=True,
        )
        authenticated_user = AuthenticatedUser(user=user, claims={})

        # Allow both ADMIN and OPERATOR
        check_role = require_role(UserRole.ADMIN, UserRole.OPERATOR)

        result = await check_role(authenticated_user)
        assert result == authenticated_user

    @pytest.mark.asyncio
    async def test_denies_user_with_wrong_role(self) -> None:
        """User without required role gets 403."""
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            external_id="user123",
            email="viewer@test.com",
            role=UserRole.VIEWER,
            is_active=True,
        )
        authenticated_user = AuthenticatedUser(user=user, claims={})

        # Require ADMIN
        check_role = require_role(UserRole.ADMIN)

        with pytest.raises(HTTPException) as exc_info:
            await check_role(authenticated_user)

        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert "Insufficient permissions" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_denies_viewer_when_operator_required(self) -> None:
        """VIEWER cannot access OPERATOR endpoints."""
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            external_id="user123",
            email="viewer@test.com",
            role=UserRole.VIEWER,
            is_active=True,
        )
        authenticated_user = AuthenticatedUser(user=user, claims={})

        check_role = require_role(UserRole.OPERATOR, UserRole.ADMIN)

        with pytest.raises(HTTPException) as exc_info:
            await check_role(authenticated_user)

        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.asyncio
    async def test_does_not_reveal_role_requirements_in_error(self) -> None:
        """Error message doesn't leak specific role requirements."""
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            external_id="user123",
            email="viewer@test.com",
            role=UserRole.VIEWER,
            is_active=True,
        )
        authenticated_user = AuthenticatedUser(user=user, claims={})

        check_role = require_role(UserRole.ADMIN)

        with pytest.raises(HTTPException) as exc_info:
            await check_role(authenticated_user)

        # Should not mention "admin" specifically
        assert "admin" not in exc_info.value.detail.lower()
        assert "Insufficient permissions" in exc_info.value.detail


class TestAuthenticatedUser:
    """Test AuthenticatedUser wrapper class."""

    def test_provides_user_properties(self) -> None:
        """AuthenticatedUser exposes user properties."""
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        user = User(
            id=user_id,
            tenant_id=tenant_id,
            external_id="user123",
            email="test@example.com",
            role=UserRole.ADMIN,
            is_active=True,
        )
        claims = {"sub": "user123", "tenant_id": str(tenant_id)}

        authenticated_user = AuthenticatedUser(user=user, claims=claims)

        assert authenticated_user.id == user_id
        assert authenticated_user.tenant_id == tenant_id
        assert authenticated_user.role == UserRole.ADMIN
        assert authenticated_user.email == "test@example.com"

    def test_provides_access_to_raw_claims(self) -> None:
        """AuthenticatedUser exposes raw JWT claims."""
        user = User(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            external_id="user123",
            email="test@example.com",
            role=UserRole.VIEWER,
            is_active=True,
        )
        claims = {
            "sub": "user123",
            "tenant_id": str(uuid.uuid4()),
            "custom_claim": "custom_value",
        }

        authenticated_user = AuthenticatedUser(user=user, claims=claims)

        assert authenticated_user.claims["custom_claim"] == "custom_value"
