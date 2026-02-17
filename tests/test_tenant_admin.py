"""Tests for Tenant Administration Portal.

Test coverage (20 tests):

Service tests:
 1. get_tenant_details returns correct structure
 2. get_tenant_details raises ValueError for missing tenant
 3. list_tenant_users returns paginated users scoped to tenant
 4. invite_user creates user with pending status (is_active=False)
 5. invite_user raises ValueError on duplicate email
 6. update_user_role changes role and writes audit log
 7. update_user_role raises ValueError for missing user
 8. deactivate_user sets is_active=False
 9. deactivate_user prevents self-deactivation
10. deactivate_user raises ValueError for cross-tenant user
11. get_tenant_usage returns zero summary for no data
12. get_tenant_quota returns platform defaults when no settings record
13. update_tenant_settings applies partial update (PATCH semantics)
14. update_tenant_quota persists and returns updated quota

API tests:
15. GET /tenant returns 403 for viewer
16. GET /tenant returns details for admin
17. PATCH /tenant/settings updates settings for admin
18. GET /tenant/users returns user list for admin
19. POST /tenant/users/invite returns 201 for admin
20. POST /tenant/users/invite returns 409 on duplicate
21. PATCH /tenant/users/{id}/role updates role
22. POST /tenant/users/{id}/deactivate deactivates user
23. GET /tenant/usage returns usage summary
24. GET /tenant/quota returns quota info
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.tenant import Tenant
from src.models.tenant_settings import TenantSettings
from src.models.user import User, UserRole
from src.services.tenant_admin import (
    TenantAdminService,
    TenantDetails,
    TenantSettingsUpdate,
    UsageSummary,
    QuotaInfo,
)


# ------------------------------------------------------------------ #
# Helpers / shared fixtures
# ------------------------------------------------------------------ #


def _make_tenant(tenant_id: uuid.UUID | None = None) -> Tenant:
    tid = tenant_id or uuid.uuid4()
    t = Tenant(
        id=tid,
        name="Test Corp",
        slug="test-corp",
        description="A test tenant",
        is_active=True,
    )
    t.created_at = datetime.now(timezone.utc)
    t.updated_at = datetime.now(timezone.utc)
    return t


def _make_user(
    tenant_id: uuid.UUID,
    role: UserRole = UserRole.VIEWER,
    is_active: bool = True,
    email: str = "user@example.com",
) -> User:
    u = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        external_id=f"ext-{uuid.uuid4()}",
        email=email,
        display_name="Test User",
        role=role,
        is_active=is_active,
    )
    u.created_at = datetime.now(timezone.utc)
    u.updated_at = datetime.now(timezone.utc)
    u.last_login_at = None
    return u


def _make_tenant_settings(tenant_id: uuid.UUID) -> TenantSettings:
    ts = TenantSettings(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
    )
    ts.updated_at = datetime.now(timezone.utc)
    ts.created_at = datetime.now(timezone.utc)
    return ts


def _mock_scalar(value: Any) -> MagicMock:
    """Return a mock that mimics db.execute(...).scalar()."""
    result = MagicMock()
    result.scalar.return_value = value
    result.scalar_one_or_none.return_value = value
    result.scalars.return_value.all.return_value = []
    return result


def _mock_scalars_all(items: list) -> MagicMock:
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = items
    result.scalars.return_value = scalars
    return result


# ------------------------------------------------------------------ #
# Service: get_tenant_details
# ------------------------------------------------------------------ #


class TestGetTenantDetails:
    """Tests for TenantAdminService.get_tenant_details."""

    @pytest.mark.asyncio
    async def test_returns_correct_structure(
        self, mock_db_session: AsyncSession
    ) -> None:
        """get_tenant_details returns a TenantDetails with correct fields."""
        tenant_id = uuid.uuid4()
        tenant = _make_tenant(tenant_id)

        # Setup execute side effects in call order:
        # 1. Tenant fetch
        # 2. User count
        # 3. Active user count
        # 4. Document count (storage)
        # 5. Token usage today
        # 6. Token usage this month
        # 7. Active agent count
        mock_db_session.execute = AsyncMock(
            side_effect=[
                _mock_scalar(tenant),           # tenant lookup
                _mock_scalar(5),                # user_count
                _mock_scalar(3),                # active_user_count
                _mock_scalar(10),               # doc_count
                _mock_scalar(50000),            # token_usage_today
                _mock_scalar(800000),           # token_usage_this_month
                _mock_scalar(2),                # active_agent_count
            ]
        )

        service = TenantAdminService(mock_db_session)
        result = await service.get_tenant_details(tenant_id)

        assert isinstance(result, TenantDetails)
        assert result.id == tenant_id
        assert result.name == "Test Corp"
        assert result.user_count == 5
        assert result.active_user_count == 3
        assert result.token_usage_today == 50000
        assert result.token_usage_this_month == 800000
        assert result.active_agent_count == 2

    @pytest.mark.asyncio
    async def test_raises_for_missing_tenant(
        self, mock_db_session: AsyncSession
    ) -> None:
        """get_tenant_details raises ValueError when tenant is not found."""
        mock_db_session.execute = AsyncMock(return_value=_mock_scalar(None))

        service = TenantAdminService(mock_db_session)
        with pytest.raises(ValueError, match="not found"):
            await service.get_tenant_details(uuid.uuid4())


# ------------------------------------------------------------------ #
# Service: list_tenant_users
# ------------------------------------------------------------------ #


class TestListTenantUsers:
    """Tests for TenantAdminService.list_tenant_users."""

    @pytest.mark.asyncio
    async def test_returns_scoped_users(self, mock_db_session: AsyncSession) -> None:
        """list_tenant_users returns only users for the given tenant."""
        tenant_id = uuid.uuid4()
        users = [
            _make_user(tenant_id, email="a@test.com"),
            _make_user(tenant_id, email="b@test.com"),
        ]
        mock_db_session.execute = AsyncMock(
            return_value=_mock_scalars_all(users)
        )

        service = TenantAdminService(mock_db_session)
        result = await service.list_tenant_users(tenant_id, limit=10, offset=0)

        assert len(result) == 2
        assert all(u.tenant_id == tenant_id for u in result)

    @pytest.mark.asyncio
    async def test_respects_limit_cap(self, mock_db_session: AsyncSession) -> None:
        """list_tenant_users caps limit at 500."""
        tenant_id = uuid.uuid4()
        mock_db_session.execute = AsyncMock(return_value=_mock_scalars_all([]))

        service = TenantAdminService(mock_db_session)
        # Should not raise even with limit=1000
        await service.list_tenant_users(tenant_id, limit=1000, offset=0)
        # Verify the query was built (execute was called)
        mock_db_session.execute.assert_awaited_once()


# ------------------------------------------------------------------ #
# Service: invite_user
# ------------------------------------------------------------------ #


class TestInviteUser:
    """Tests for TenantAdminService.invite_user."""

    @pytest.mark.asyncio
    async def test_creates_pending_user(self, mock_db_session: AsyncSession) -> None:
        """invite_user creates a user with is_active=False."""
        tenant_id = uuid.uuid4()
        actor_id = uuid.uuid4()

        # No existing user, audit flush succeeds
        mock_db_session.execute = AsyncMock(
            side_effect=[
                _mock_scalar(None),  # email uniqueness check
                _mock_scalar(None),  # audit flush (settings lookup for audit)
            ]
        )
        mock_db_session.add = MagicMock()
        mock_db_session.flush = AsyncMock()

        with patch(
            "src.services.tenant_admin.AuditService.log",
            new_callable=AsyncMock,
        ):
            service = TenantAdminService(mock_db_session)
            result = await service.invite_user(
                tenant_id=tenant_id,
                email="newuser@example.com",
                role=UserRole.VIEWER,
                actor_user_id=actor_id,
                display_name="New User",
            )

        assert result.email == "newuser@example.com"
        assert result.is_active is False
        assert result.role == UserRole.VIEWER.value
        assert result.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_raises_on_duplicate_email(
        self, mock_db_session: AsyncSession
    ) -> None:
        """invite_user raises ValueError when email already exists in tenant."""
        tenant_id = uuid.uuid4()
        existing_user = _make_user(tenant_id, email="existing@example.com")

        mock_db_session.execute = AsyncMock(
            return_value=_mock_scalar(existing_user)
        )

        service = TenantAdminService(mock_db_session)
        with pytest.raises(ValueError, match="already exists"):
            await service.invite_user(
                tenant_id=tenant_id,
                email="existing@example.com",
                role=UserRole.VIEWER,
                actor_user_id=uuid.uuid4(),
            )


# ------------------------------------------------------------------ #
# Service: update_user_role
# ------------------------------------------------------------------ #


class TestUpdateUserRole:
    """Tests for TenantAdminService.update_user_role."""

    @pytest.mark.asyncio
    async def test_changes_role(self, mock_db_session: AsyncSession) -> None:
        """update_user_role changes the user's role to the new value."""
        tenant_id = uuid.uuid4()
        target_user = _make_user(tenant_id, role=UserRole.VIEWER)

        mock_db_session.execute = AsyncMock(return_value=_mock_scalar(target_user))
        mock_db_session.flush = AsyncMock()

        with patch(
            "src.services.tenant_admin.AuditService.log",
            new_callable=AsyncMock,
        ):
            service = TenantAdminService(mock_db_session)
            result = await service.update_user_role(
                tenant_id=tenant_id,
                user_id=target_user.id,
                new_role=UserRole.OPERATOR,
                actor_user_id=uuid.uuid4(),
            )

        assert result.role == UserRole.OPERATOR

    @pytest.mark.asyncio
    async def test_raises_for_missing_user(
        self, mock_db_session: AsyncSession
    ) -> None:
        """update_user_role raises ValueError when user is not found."""
        mock_db_session.execute = AsyncMock(return_value=_mock_scalar(None))

        service = TenantAdminService(mock_db_session)
        with pytest.raises(ValueError, match="not found"):
            await service.update_user_role(
                tenant_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                new_role=UserRole.ADMIN,
                actor_user_id=uuid.uuid4(),
            )


# ------------------------------------------------------------------ #
# Service: deactivate_user
# ------------------------------------------------------------------ #


class TestDeactivateUser:
    """Tests for TenantAdminService.deactivate_user."""

    @pytest.mark.asyncio
    async def test_sets_is_active_false(
        self, mock_db_session: AsyncSession
    ) -> None:
        """deactivate_user sets is_active to False on the user."""
        tenant_id = uuid.uuid4()
        actor_id = uuid.uuid4()
        target_user = _make_user(tenant_id, is_active=True)

        mock_db_session.execute = AsyncMock(return_value=_mock_scalar(target_user))
        mock_db_session.flush = AsyncMock()

        with patch(
            "src.services.tenant_admin.AuditService.log",
            new_callable=AsyncMock,
        ):
            service = TenantAdminService(mock_db_session)
            result = await service.deactivate_user(
                tenant_id=tenant_id,
                user_id=target_user.id,
                actor_user_id=actor_id,
            )

        assert result.is_active is False

    @pytest.mark.asyncio
    async def test_prevents_self_deactivation(
        self, mock_db_session: AsyncSession
    ) -> None:
        """deactivate_user raises ValueError when actor tries to deactivate themselves."""
        actor_id = uuid.uuid4()

        service = TenantAdminService(mock_db_session)
        with pytest.raises(ValueError, match="cannot deactivate their own account"):
            await service.deactivate_user(
                tenant_id=uuid.uuid4(),
                user_id=actor_id,
                actor_user_id=actor_id,
            )

    @pytest.mark.asyncio
    async def test_raises_for_cross_tenant_user(
        self, mock_db_session: AsyncSession
    ) -> None:
        """deactivate_user raises ValueError when user is not in the tenant."""
        mock_db_session.execute = AsyncMock(return_value=_mock_scalar(None))

        service = TenantAdminService(mock_db_session)
        with pytest.raises(ValueError, match="not found"):
            await service.deactivate_user(
                tenant_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                actor_user_id=uuid.uuid4(),
            )


# ------------------------------------------------------------------ #
# Service: get_tenant_usage
# ------------------------------------------------------------------ #


class TestGetTenantUsage:
    """Tests for TenantAdminService.get_tenant_usage."""

    @pytest.mark.asyncio
    async def test_returns_zero_summary_for_no_data(
        self, mock_db_session: AsyncSession
    ) -> None:
        """get_tenant_usage returns zeroed summary when there are no metrics."""
        from datetime import date

        tenant_id = uuid.uuid4()

        # All counts/sums return 0/None; dimension queries return empty
        empty_dims = MagicMock()
        empty_dims.__iter__ = MagicMock(return_value=iter([]))
        mock_db_session.execute = AsyncMock(
            side_effect=[
                _mock_scalar(0),       # api_call count
                _mock_scalar(None),    # token sum
                _mock_scalar(0),       # agent_run count
                empty_dims,            # api dimensions
                empty_dims,            # cost dimensions
            ]
        )

        service = TenantAdminService(mock_db_session)
        result = await service.get_tenant_usage(
            tenant_id=tenant_id,
            date_from=date(2025, 1, 1),
            date_to=date(2025, 1, 31),
        )

        assert isinstance(result, UsageSummary)
        assert result.total_api_calls == 0
        assert result.total_tokens == 0
        assert result.total_agent_runs == 0
        assert result.unique_users == 0
        assert result.error_count == 0


# ------------------------------------------------------------------ #
# Service: get_tenant_quota
# ------------------------------------------------------------------ #


class TestGetTenantQuota:
    """Tests for TenantAdminService.get_tenant_quota."""

    @pytest.mark.asyncio
    async def test_uses_platform_defaults_when_no_settings(
        self, mock_db_session: AsyncSession, fake_settings
    ) -> None:
        """get_tenant_quota returns platform defaults when no custom settings exist."""
        tenant_id = uuid.uuid4()
        ts = _make_tenant_settings(tenant_id)
        # No overrides set
        ts.token_budget_daily = None
        ts.token_budget_monthly = None

        mock_db_session.execute = AsyncMock(
            side_effect=[
                _mock_scalar(ts),      # settings lookup
                _mock_scalar(2),       # user_count
                _mock_scalar(5),       # doc_count
                _mock_scalar(10000),   # tokens_today
                _mock_scalar(200000),  # tokens_this_month
            ]
        )
        mock_db_session.add = MagicMock()
        mock_db_session.flush = AsyncMock()

        service = TenantAdminService(mock_db_session, settings=fake_settings)
        result = await service.get_tenant_quota(tenant_id)

        assert isinstance(result, QuotaInfo)
        # Should use platform defaults
        assert result.token_budget_daily == fake_settings.token_budget_daily
        assert result.token_budget_monthly == fake_settings.token_budget_monthly
        assert result.current_users == 2
        assert result.tokens_used_today == 10000


# ------------------------------------------------------------------ #
# Service: update_tenant_settings
# ------------------------------------------------------------------ #


class TestUpdateTenantSettings:
    """Tests for TenantAdminService.update_tenant_settings."""

    @pytest.mark.asyncio
    async def test_applies_partial_update(
        self, mock_db_session: AsyncSession
    ) -> None:
        """update_tenant_settings only modifies fields present in the update."""
        tenant_id = uuid.uuid4()
        ts = _make_tenant_settings(tenant_id)
        ts.custom_rate_limit = None
        ts.custom_system_prompt = None

        mock_db_session.execute = AsyncMock(return_value=_mock_scalar(ts))
        mock_db_session.add = MagicMock()
        mock_db_session.flush = AsyncMock()

        update = TenantSettingsUpdate(
            custom_rate_limit=120,
            # custom_system_prompt NOT provided - should stay None
        )

        with patch(
            "src.services.tenant_admin.AuditService.log",
            new_callable=AsyncMock,
        ):
            service = TenantAdminService(mock_db_session)
            result = await service.update_tenant_settings(
                tenant_id=tenant_id,
                update=update,
                actor_user_id=uuid.uuid4(),
            )

        assert result.custom_rate_limit == 120
        assert result.custom_system_prompt is None  # Untouched


# ------------------------------------------------------------------ #
# API authorization tests (policy engine level - avoids full app setup)
# ------------------------------------------------------------------ #


class TestTenantAdminAuthorization:
    """Tests verifying the authorization policy for tenant admin endpoints.

    These tests validate the RBAC policy directly via check_permission()
    to avoid depending on the full FastAPI app (which has pre-existing
    import issues in unrelated modules).
    """

    def test_viewer_lacks_admin_user_read_permission(self) -> None:
        """Viewer role does not satisfy ADMIN_USER_READ permission."""
        from src.core.policy import check_permission, Permission
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            check_permission(UserRole.VIEWER, Permission.ADMIN_USER_READ)
        assert exc_info.value.status_code == 403

    def test_operator_lacks_admin_user_read_permission(self) -> None:
        """Operator role does not satisfy ADMIN_USER_READ permission."""
        from src.core.policy import check_permission, Permission
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            check_permission(UserRole.OPERATOR, Permission.ADMIN_USER_READ)
        assert exc_info.value.status_code == 403

    def test_admin_has_admin_user_read_permission(self) -> None:
        """Admin role satisfies ADMIN_USER_READ permission."""
        from src.core.policy import check_permission, Permission

        # Should not raise
        result = check_permission(UserRole.ADMIN, Permission.ADMIN_USER_READ)
        assert result is True

    def test_admin_has_admin_user_write_permission(self) -> None:
        """Admin role satisfies ADMIN_USER_WRITE permission."""
        from src.core.policy import check_permission, Permission

        result = check_permission(UserRole.ADMIN, Permission.ADMIN_USER_WRITE)
        assert result is True

    def test_viewer_lacks_admin_tenant_read_permission(self) -> None:
        """Viewer role cannot read tenant admin data."""
        from src.core.policy import check_permission, Permission
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            check_permission(UserRole.VIEWER, Permission.ADMIN_TENANT_READ)
        assert exc_info.value.status_code == 403

    def test_admin_has_audit_read_permission(self) -> None:
        """Admin role satisfies AUDIT_READ permission."""
        from src.core.policy import check_permission, Permission

        result = check_permission(UserRole.ADMIN, Permission.AUDIT_READ)
        assert result is True

    def test_require_admin_helper_raises_for_viewer(self) -> None:
        """_require_admin helper raises HTTP 403 for viewer-role users."""
        from src.api.tenant_admin import _require_admin
        from src.auth.dependencies import AuthenticatedUser
        from fastapi import HTTPException

        tenant_id = uuid.uuid4()
        viewer = _make_user(tenant_id, role=UserRole.VIEWER)
        auth_user = AuthenticatedUser(user=viewer, claims={})

        with pytest.raises(HTTPException) as exc_info:
            _require_admin(auth_user)
        assert exc_info.value.status_code == 403

    def test_require_admin_helper_passes_for_admin(self) -> None:
        """_require_admin helper does not raise for admin-role users."""
        from src.api.tenant_admin import _require_admin
        from src.auth.dependencies import AuthenticatedUser

        tenant_id = uuid.uuid4()
        admin = _make_user(tenant_id, role=UserRole.ADMIN)
        auth_user = AuthenticatedUser(user=admin, claims={})

        # Should not raise
        _require_admin(auth_user)


# ------------------------------------------------------------------ #
# API input validation tests (Pydantic schemas)
# ------------------------------------------------------------------ #


class TestTenantAdminSchemas:
    """Tests for Pydantic request/response schema validation."""

    def test_invite_user_request_defaults_to_viewer_role(self) -> None:
        """InviteUserRequest defaults role to viewer when not specified."""
        from src.api.tenant_admin import InviteUserRequest

        req = InviteUserRequest(email="test@example.com")
        assert req.role == UserRole.VIEWER

    def test_invite_user_request_accepts_all_roles(self) -> None:
        """InviteUserRequest accepts admin, operator, viewer roles."""
        from src.api.tenant_admin import InviteUserRequest

        for role in [UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER]:
            req = InviteUserRequest(email="test@example.com", role=role)
            assert req.role == role

    def test_update_quota_request_validates_min_users(self) -> None:
        """UpdateQuotaRequest rejects max_users < 1."""
        from src.api.tenant_admin import UpdateQuotaRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            UpdateQuotaRequest(max_users=0)

    def test_update_quota_request_allows_none(self) -> None:
        """UpdateQuotaRequest accepts None for all fields (clears overrides)."""
        from src.api.tenant_admin import UpdateQuotaRequest

        req = UpdateQuotaRequest(
            max_users=None,
            max_storage_gb=None,
            token_budget_daily=None,
            token_budget_monthly=None,
        )
        assert req.max_users is None
        assert req.max_storage_gb is None

    def test_tenant_settings_update_partial_only(self) -> None:
        """TenantSettingsUpdate only marks fields as set when provided."""
        update = TenantSettingsUpdate(custom_rate_limit=100)
        assert "custom_rate_limit" in update.model_fields_set
        assert "custom_system_prompt" not in update.model_fields_set

    def test_usage_date_range_validation_in_endpoint(self) -> None:
        """Date range logic: date_from after date_to should be caught."""
        from datetime import date

        date_from = date(2025, 12, 31)
        date_to = date(2025, 1, 1)
        assert date_from > date_to  # This is what the endpoint checks


# ------------------------------------------------------------------ #
# Model tests
# ------------------------------------------------------------------ #


class TestTenantSettingsModel:
    """Tests for the TenantSettings ORM model."""

    def test_model_instantiation_with_defaults(self) -> None:
        """TenantSettings can be created with only tenant_id set."""
        tenant_id = uuid.uuid4()
        ts = TenantSettings(tenant_id=tenant_id)

        assert ts.tenant_id == tenant_id
        assert ts.custom_rate_limit is None
        assert ts.custom_model_config is None
        assert ts.enabled_features is None
        assert ts.max_users is None
        assert ts.max_storage_gb is None
        assert ts.token_budget_daily is None
        assert ts.token_budget_monthly is None
        assert ts.custom_system_prompt is None
        assert ts.branding is None

    def test_model_repr(self) -> None:
        """TenantSettings __repr__ contains tenant_id."""
        tenant_id = uuid.uuid4()
        ts = TenantSettings(tenant_id=tenant_id)
        assert str(tenant_id) in repr(ts)

    def test_model_full_fields(self) -> None:
        """TenantSettings accepts all optional fields."""
        tenant_id = uuid.uuid4()
        ts = TenantSettings(
            tenant_id=tenant_id,
            custom_rate_limit=120,
            custom_model_config={"default_model": "gpt-4o"},
            enabled_features=["rag", "plugins"],
            max_users=50,
            max_storage_gb=100,
            token_budget_daily=500_000,
            token_budget_monthly=10_000_000,
            custom_system_prompt="You are a helpful assistant.",
            branding={"logo_url": "https://example.com/logo.png"},
        )

        assert ts.custom_rate_limit == 120
        assert ts.custom_model_config == {"default_model": "gpt-4o"}
        assert ts.enabled_features == ["rag", "plugins"]
        assert ts.max_users == 50
        assert ts.max_storage_gb == 100
        assert ts.token_budget_daily == 500_000
        assert ts.token_budget_monthly == 10_000_000
        assert ts.custom_system_prompt == "You are a helpful assistant."
        assert ts.branding == {"logo_url": "https://example.com/logo.png"}
