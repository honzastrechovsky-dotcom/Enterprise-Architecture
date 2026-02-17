"""Tenant Administration Service.

Provides all business logic for the Tenant Admin Portal:
- Tenant details (users count, storage, token usage, active agents)
- Tenant settings management (rate limits, model config, features)
- User management within the tenant (list, invite, role change, deactivate)
- Usage summaries and quota information

All operations are tenant-scoped and enforce the caller's tenant_id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.core.audit import AuditService
from src.core.policy import apply_tenant_filter
from src.models.analytics import MetricType, UsageMetric
from src.models.audit import AuditStatus
from src.models.document import Document
from src.models.tenant import Tenant
from src.models.tenant_settings import TenantSettings
from src.models.user import User, UserRole

log = structlog.get_logger(__name__)


# ------------------------------------------------------------------ #
# Response models (Pydantic - not ORM)
# ------------------------------------------------------------------ #


class TenantDetails(BaseModel):
    """Enriched tenant view for the admin portal dashboard."""

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    # Computed stats
    user_count: int
    active_user_count: int
    storage_used_gb: float
    token_usage_today: int
    token_usage_this_month: int
    active_agent_count: int


class UsageSummary(BaseModel):
    """Usage data for the tenant dashboard."""

    date_from: date
    date_to: date
    total_api_calls: int
    total_tokens: int
    total_agent_runs: int
    unique_users: int
    avg_response_time_ms: float
    error_count: int
    cost_estimate: float


class QuotaInfo(BaseModel):
    """Quota limits and current consumption."""

    # Limits (None = unlimited / use platform default)
    max_users: int | None
    max_storage_gb: int | None
    token_budget_daily: int
    token_budget_monthly: int
    custom_rate_limit: int | None

    # Consumption
    current_users: int
    current_storage_gb: float
    tokens_used_today: int
    tokens_used_this_month: int

    # Derived
    users_remaining: int | None
    storage_remaining_gb: float | None
    daily_tokens_remaining: int
    monthly_tokens_remaining: int


class UserInviteResult(BaseModel):
    """Result of an invite operation."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    is_active: bool
    created_at: datetime


class TenantSettingsUpdate(BaseModel):
    """Input model for updating tenant settings (all fields optional)."""

    custom_rate_limit: int | None = None
    custom_model_config: dict[str, Any] | None = None
    enabled_features: list[str] | None = None
    max_users: int | None = None
    max_storage_gb: int | None = None
    token_budget_daily: int | None = None
    token_budget_monthly: int | None = None
    custom_system_prompt: str | None = None
    branding: dict[str, Any] | None = None


class TenantSettingsResponse(BaseModel):
    """Full settings record returned to the caller."""

    tenant_id: uuid.UUID
    custom_rate_limit: int | None
    custom_model_config: dict[str, Any] | None
    enabled_features: list[str] | None
    max_users: int | None
    max_storage_gb: int | None
    token_budget_daily: int | None
    token_budget_monthly: int | None
    custom_system_prompt: str | None
    branding: dict[str, Any] | None
    updated_at: datetime


# ------------------------------------------------------------------ #
# Service
# ------------------------------------------------------------------ #


class TenantAdminService:
    """Business logic for the Tenant Administration Portal."""

    def __init__(self, db: AsyncSession, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self._audit = AuditService(db)

    # ---------------------------------------------------------------- #
    # Tenant details
    # ---------------------------------------------------------------- #

    async def get_tenant_details(self, tenant_id: uuid.UUID) -> TenantDetails:
        """Return enriched tenant information including computed stats.

        Args:
            tenant_id: The tenant to inspect.

        Returns:
            TenantDetails with counts and resource usage.

        Raises:
            ValueError: If the tenant does not exist.
        """
        result = await self.db.execute(
            select(Tenant).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
        )
        tenant = result.scalar_one_or_none()
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found")

        # User counts
        user_count_result = await self.db.execute(
            select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
        )
        user_count = user_count_result.scalar() or 0

        active_user_result = await self.db.execute(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant_id, User.is_active.is_(True))
        )
        active_user_count = active_user_result.scalar() or 0

        # Storage: sum document sizes (approximate via chunk count * avg size)
        # We use a heuristic: count documents and assume average size
        storage_result = await self.db.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.tenant_id == tenant_id)
        )
        doc_count = storage_result.scalar() or 0
        # Approximate: average doc is ~1 MB
        storage_used_gb = round(doc_count * 0.001, 4)

        # Token usage today
        today = datetime.now(UTC).date()
        today_start = datetime(today.year, today.month, today.day, tzinfo=UTC)
        token_today_result = await self.db.execute(
            select(func.sum(UsageMetric.value)).where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= today_start,
            )
        )
        token_usage_today = int(token_today_result.scalar() or 0)

        # Token usage this month
        month_start = datetime(today.year, today.month, 1, tzinfo=UTC)
        token_month_result = await self.db.execute(
            select(func.sum(UsageMetric.value)).where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= month_start,
            )
        )
        token_usage_this_month = int(token_month_result.scalar() or 0)

        # Active agent count: distinct agent_ids in AGENT_RUN metrics today
        agent_result = await self.db.execute(
            select(func.count(func.distinct(UsageMetric.dimensions["agent_id"].as_string()))).where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.AGENT_RUN,
                UsageMetric.timestamp >= today_start,
            )
        )
        active_agent_count = int(agent_result.scalar() or 0)

        return TenantDetails(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            description=tenant.description,
            is_active=tenant.is_active,
            created_at=tenant.created_at,
            updated_at=tenant.updated_at,
            user_count=user_count,
            active_user_count=active_user_count,
            storage_used_gb=storage_used_gb,
            token_usage_today=token_usage_today,
            token_usage_this_month=token_usage_this_month,
            active_agent_count=active_agent_count,
        )

    # ---------------------------------------------------------------- #
    # Tenant settings
    # ---------------------------------------------------------------- #

    async def _get_or_create_settings(self, tenant_id: uuid.UUID) -> TenantSettings:
        """Return the TenantSettings record, creating it if absent."""
        result = await self.db.execute(
            select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
        )
        settings = result.scalar_one_or_none()
        if settings is None:
            settings = TenantSettings(tenant_id=tenant_id)
            self.db.add(settings)
            await self.db.flush()
        return settings

    async def get_tenant_settings(self, tenant_id: uuid.UUID) -> TenantSettingsResponse:
        """Return current settings for a tenant (creates defaults if absent)."""
        settings = await self._get_or_create_settings(tenant_id)
        return TenantSettingsResponse(
            tenant_id=settings.tenant_id,
            custom_rate_limit=settings.custom_rate_limit,
            custom_model_config=settings.custom_model_config,
            enabled_features=settings.enabled_features,
            max_users=settings.max_users,
            max_storage_gb=settings.max_storage_gb,
            token_budget_daily=settings.token_budget_daily,
            token_budget_monthly=settings.token_budget_monthly,
            custom_system_prompt=settings.custom_system_prompt,
            branding=settings.branding,
            updated_at=settings.updated_at,
        )

    async def update_tenant_settings(
        self,
        tenant_id: uuid.UUID,
        update: TenantSettingsUpdate,
        actor_user_id: uuid.UUID,
    ) -> TenantSettingsResponse:
        """Apply a partial settings update for a tenant.

        Only fields explicitly included in *update* are modified (PATCH
        semantics). Fields left at their default (None in the input) are
        not written unless explicitly set.

        Args:
            tenant_id: Target tenant.
            update: Partial update payload.
            actor_user_id: User performing the update (for audit log).

        Returns:
            Updated TenantSettingsResponse.
        """
        settings = await self._get_or_create_settings(tenant_id)

        changed: dict[str, Any] = {}

        # Apply only provided (non-None) fields.  We use model_fields_set
        # so callers can explicitly set a field to null to clear it.
        update_data = update.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(settings, field, value)
            changed[field] = value

        settings.updated_at = datetime.now(UTC)

        await self._audit.log(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            action="tenant_admin.settings.update",
            resource_type="tenant_settings",
            resource_id=str(tenant_id),
            status=AuditStatus.SUCCESS,
            extra={"changed_fields": list(changed.keys())},
        )

        log.info(
            "tenant_admin.settings_updated",
            tenant_id=str(tenant_id),
            fields=list(changed.keys()),
        )

        return TenantSettingsResponse(
            tenant_id=settings.tenant_id,
            custom_rate_limit=settings.custom_rate_limit,
            custom_model_config=settings.custom_model_config,
            enabled_features=settings.enabled_features,
            max_users=settings.max_users,
            max_storage_gb=settings.max_storage_gb,
            token_budget_daily=settings.token_budget_daily,
            token_budget_monthly=settings.token_budget_monthly,
            custom_system_prompt=settings.custom_system_prompt,
            branding=settings.branding,
            updated_at=settings.updated_at,
        )

    # ---------------------------------------------------------------- #
    # User management
    # ---------------------------------------------------------------- #

    async def list_tenant_users(
        self,
        tenant_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[User]:
        """Return paginated users for a tenant.

        Args:
            tenant_id: Tenant to query.
            limit: Maximum records to return (1-500).
            offset: Number of records to skip.

        Returns:
            List of User ORM objects.
        """
        stmt = (
            apply_tenant_filter(select(User), User, tenant_id)
            .order_by(User.created_at.desc())
            .offset(offset)
            .limit(min(limit, 500))
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def invite_user(
        self,
        tenant_id: uuid.UUID,
        email: str,
        role: UserRole,
        actor_user_id: uuid.UUID,
        display_name: str | None = None,
    ) -> UserInviteResult:
        """Create a pending user invitation.

        A user created by this method has a synthetic external_id derived
        from the email (prefixed with "invite:") so the record can be
        matched to an identity-provider login later.  The record is
        inactive until the user completes sign-up.

        Args:
            tenant_id: Tenant the user will belong to.
            email: Email address to invite.
            role: Role to assign upon acceptance.
            actor_user_id: Admin performing the invite (for audit).
            display_name: Optional display name.

        Returns:
            UserInviteResult with the new user's details.

        Raises:
            ValueError: If a user with the same email already exists in the tenant.
        """
        # Check for existing user with the same email in this tenant
        existing = await self.db.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.email == email,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"User with email {email!r} already exists in this tenant")

        # Synthetic external_id for invite flow
        synthetic_external_id = f"invite:{email}"

        now = datetime.now(UTC)
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            external_id=synthetic_external_id,
            email=email,
            display_name=display_name,
            role=role,
            is_active=False,  # Pending invitation
            created_at=now,
            updated_at=now,
        )
        self.db.add(user)
        await self.db.flush()

        await self._audit.log(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            action="tenant_admin.user.invite",
            resource_type="user",
            resource_id=str(user.id),
            status=AuditStatus.SUCCESS,
            extra={"email": email, "role": role.value},
        )

        log.info(
            "tenant_admin.user_invited",
            tenant_id=str(tenant_id),
            email=email,
            role=role.value,
        )

        return UserInviteResult(
            id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            display_name=user.display_name,
            role=user.role.value,
            is_active=user.is_active,
            created_at=user.created_at,
        )

    async def update_user_role(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        new_role: UserRole,
        actor_user_id: uuid.UUID,
    ) -> User:
        """Change a user's role within the tenant.

        Args:
            tenant_id: Tenant scope (ensures cross-tenant isolation).
            user_id: Target user.
            new_role: The role to assign.
            actor_user_id: Admin performing the change (for audit).

        Returns:
            Updated User ORM object.

        Raises:
            ValueError: If the user is not found within the tenant.
        """
        stmt = apply_tenant_filter(
            select(User).where(User.id == user_id),
            User,
            tenant_id,
        )
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()
        if user is None:
            raise ValueError(f"User {user_id} not found in tenant {tenant_id}")

        old_role = user.role
        user.role = new_role

        await self._audit.log(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            action="tenant_admin.user.role_change",
            resource_type="user",
            resource_id=str(user_id),
            status=AuditStatus.SUCCESS,
            extra={"old_role": old_role.value, "new_role": new_role.value},
        )

        log.info(
            "tenant_admin.user_role_changed",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            old_role=old_role.value,
            new_role=new_role.value,
        )

        return user

    async def deactivate_user(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> User:
        """Deactivate a user within the tenant.

        Soft-deactivation: sets is_active=False.  The user record is kept
        for audit trail purposes.

        Args:
            tenant_id: Tenant scope.
            user_id: Target user.
            actor_user_id: Admin performing the action (for audit).

        Returns:
            Updated User ORM object.

        Raises:
            ValueError: If the user is not found within the tenant.
            ValueError: If the actor attempts to deactivate themselves.
        """
        if user_id == actor_user_id:
            raise ValueError("Admins cannot deactivate their own account")

        stmt = apply_tenant_filter(
            select(User).where(User.id == user_id),
            User,
            tenant_id,
        )
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()
        if user is None:
            raise ValueError(f"User {user_id} not found in tenant {tenant_id}")

        user.is_active = False

        await self._audit.log(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            action="tenant_admin.user.deactivate",
            resource_type="user",
            resource_id=str(user_id),
            status=AuditStatus.SUCCESS,
            extra={"email": user.email},
        )

        log.info(
            "tenant_admin.user_deactivated",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
        )

        return user

    # ---------------------------------------------------------------- #
    # Usage
    # ---------------------------------------------------------------- #

    async def get_tenant_usage(
        self,
        tenant_id: uuid.UUID,
        date_from: date,
        date_to: date,
    ) -> UsageSummary:
        """Return aggregated usage metrics for a date range.

        Args:
            tenant_id: Tenant to query.
            date_from: Start date (inclusive).
            date_to: End date (inclusive).

        Returns:
            UsageSummary with aggregated metrics.
        """
        start_dt = datetime(
            date_from.year, date_from.month, date_from.day, tzinfo=UTC
        )
        end_dt = datetime(
            date_to.year, date_to.month, date_to.day, 23, 59, 59, tzinfo=UTC
        )

        # API calls
        api_call_result = await self.db.execute(
            select(func.count())
            .select_from(UsageMetric)
            .where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.API_CALL,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        total_api_calls = int(api_call_result.scalar() or 0)

        # Token usage
        token_result = await self.db.execute(
            select(func.sum(UsageMetric.value)).where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        total_tokens = int(token_result.scalar() or 0)

        # Agent runs
        agent_result = await self.db.execute(
            select(func.count())
            .select_from(UsageMetric)
            .where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.AGENT_RUN,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        total_agent_runs = int(agent_result.scalar() or 0)

        # Pull all API call metrics for derived stats (unique users, latency, errors)
        all_api_metrics = await self.db.execute(
            select(UsageMetric.dimensions).where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.API_CALL,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        api_dims = [row[0] for row in all_api_metrics]

        user_ids: set[str] = set()
        response_times: list[float] = []
        error_count = 0

        for dims in api_dims:
            if dims.get("user_id"):
                user_ids.add(dims["user_id"])
            if "response_time_ms" in dims:
                response_times.append(float(dims["response_time_ms"]))
            if dims.get("status_code", 0) >= 400:
                error_count += 1

        avg_response_time_ms = (
            sum(response_times) / len(response_times) if response_times else 0.0
        )

        # Cost estimate from token usage dimensions
        cost_dims_result = await self.db.execute(
            select(UsageMetric.dimensions).where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        cost_estimate = sum(
            float(row[0].get("cost", 0.0)) for row in cost_dims_result
        )

        return UsageSummary(
            date_from=date_from,
            date_to=date_to,
            total_api_calls=total_api_calls,
            total_tokens=total_tokens,
            total_agent_runs=total_agent_runs,
            unique_users=len(user_ids),
            avg_response_time_ms=avg_response_time_ms,
            error_count=error_count,
            cost_estimate=cost_estimate,
        )

    # ---------------------------------------------------------------- #
    # Quota
    # ---------------------------------------------------------------- #

    async def get_tenant_quota(self, tenant_id: uuid.UUID) -> QuotaInfo:
        """Return quota limits and current consumption.

        Args:
            tenant_id: Tenant to query.

        Returns:
            QuotaInfo combining configured limits with actual usage.
        """
        settings_record = await self._get_or_create_settings(tenant_id)

        # Effective limits (settings override > platform default)
        effective_daily = (
            settings_record.token_budget_daily or self.settings.token_budget_daily
        )
        effective_monthly = (
            settings_record.token_budget_monthly or self.settings.token_budget_monthly
        )

        # Current user count
        user_count_result = await self.db.execute(
            select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
        )
        current_users = int(user_count_result.scalar() or 0)

        # Current storage (heuristic)
        storage_result = await self.db.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.tenant_id == tenant_id)
        )
        doc_count = int(storage_result.scalar() or 0)
        current_storage_gb = round(doc_count * 0.001, 4)

        # Token usage today
        today = datetime.now(UTC).date()
        today_start = datetime(today.year, today.month, today.day, tzinfo=UTC)
        token_today_result = await self.db.execute(
            select(func.sum(UsageMetric.value)).where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= today_start,
            )
        )
        tokens_used_today = int(token_today_result.scalar() or 0)

        # Token usage this month
        month_start = datetime(today.year, today.month, 1, tzinfo=UTC)
        token_month_result = await self.db.execute(
            select(func.sum(UsageMetric.value)).where(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= month_start,
            )
        )
        tokens_used_this_month = int(token_month_result.scalar() or 0)

        # Derived: remaining
        users_remaining: int | None = None
        if settings_record.max_users is not None:
            users_remaining = max(0, settings_record.max_users - current_users)

        storage_remaining_gb: float | None = None
        if settings_record.max_storage_gb is not None:
            storage_remaining_gb = max(
                0.0, settings_record.max_storage_gb - current_storage_gb
            )

        return QuotaInfo(
            max_users=settings_record.max_users,
            max_storage_gb=settings_record.max_storage_gb,
            token_budget_daily=effective_daily,
            token_budget_monthly=effective_monthly,
            custom_rate_limit=settings_record.custom_rate_limit,
            current_users=current_users,
            current_storage_gb=current_storage_gb,
            tokens_used_today=tokens_used_today,
            tokens_used_this_month=tokens_used_this_month,
            users_remaining=users_remaining,
            storage_remaining_gb=storage_remaining_gb,
            daily_tokens_remaining=max(0, effective_daily - tokens_used_today),
            monthly_tokens_remaining=max(0, effective_monthly - tokens_used_this_month),
        )

    async def update_tenant_quota(
        self,
        tenant_id: uuid.UUID,
        max_users: int | None,
        max_storage_gb: int | None,
        token_budget_daily: int | None,
        token_budget_monthly: int | None,
        actor_user_id: uuid.UUID,
    ) -> QuotaInfo:
        """Update quota settings for a tenant.

        Args:
            tenant_id: Target tenant.
            max_users: New user limit (None clears override).
            max_storage_gb: New storage limit in GiB (None clears override).
            token_budget_daily: Daily token budget (None uses platform default).
            token_budget_monthly: Monthly token budget (None uses platform default).
            actor_user_id: Admin performing the change (for audit).

        Returns:
            Updated QuotaInfo.
        """
        settings_record = await self._get_or_create_settings(tenant_id)

        settings_record.max_users = max_users
        settings_record.max_storage_gb = max_storage_gb
        settings_record.token_budget_daily = token_budget_daily
        settings_record.token_budget_monthly = token_budget_monthly
        settings_record.updated_at = datetime.now(UTC)

        await self._audit.log(
            tenant_id=tenant_id,
            user_id=actor_user_id,
            action="tenant_admin.quota.update",
            resource_type="tenant_settings",
            resource_id=str(tenant_id),
            status=AuditStatus.SUCCESS,
            extra={
                "max_users": max_users,
                "max_storage_gb": max_storage_gb,
                "token_budget_daily": token_budget_daily,
                "token_budget_monthly": token_budget_monthly,
            },
        )

        return await self.get_tenant_quota(tenant_id)
