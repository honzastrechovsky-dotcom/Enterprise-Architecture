"""Token budget management for cost control.

The BudgetManager tracks token usage per tenant and enforces daily/monthly
limits. It provides:
- Usage tracking by model tier
- Budget limit enforcement
- Alerting at threshold levels (80%, 95%)
- Savings reporting from intelligent routing

BudgetManager uses in-memory storage (non-persistent). PersistentBudgetManager
extends it to persist state in PostgreSQL via SQLAlchemy async sessions, making
budgets durable across restarts and safe under concurrent workers.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.token_budget import TokenBudgetRecord, TokenUsageRecord

if TYPE_CHECKING:
    from src.agent.model_router.router import ModelTier

log = structlog.get_logger(__name__)


@dataclass
class TokenBudget:
    """Token budget and usage for a tenant.

    Attributes:
        tenant_id: UUID of the tenant
        daily_limit: Maximum tokens per day
        monthly_limit: Maximum tokens per month
        current_daily: Tokens used today
        current_monthly: Tokens used this month
        last_reset_date: Date of last daily reset (YYYY-MM-DD)
        last_reset_month: Month of last monthly reset (YYYY-MM)
    """

    tenant_id: uuid.UUID
    daily_limit: int
    monthly_limit: int
    current_daily: int = 0
    current_monthly: int = 0
    last_reset_date: str = ""
    last_reset_month: str = ""

    def __post_init__(self) -> None:
        """Initialize reset timestamps if not provided."""
        if not self.last_reset_date:
            self.last_reset_date = datetime.now(UTC).strftime("%Y-%m-%d")
        if not self.last_reset_month:
            self.last_reset_month = datetime.now(UTC).strftime("%Y-%m")


@dataclass
class UsageRecord:
    """Record of token usage for savings calculation."""

    timestamp: datetime
    model_tier: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    complexity_score: float


class BudgetManager:
    """Manages token budgets and enforces limits per tenant.

    Tracks usage, enforces limits, and provides savings reports showing
    how much was saved by routing to cheaper models.
    """

    # Alert thresholds (percentage of limit)
    WARNING_THRESHOLD = 0.80  # 80%
    CRITICAL_THRESHOLD = 0.95  # 95%

    def __init__(
        self,
        default_daily_limit: int = 1_000_000,
        default_monthly_limit: int = 20_000_000,
    ) -> None:
        """Initialize budget manager.

        Args:
            default_daily_limit: Default daily token limit per tenant
            default_monthly_limit: Default monthly token limit per tenant
        """
        self._default_daily = default_daily_limit
        self._default_monthly = default_monthly_limit

        # In-memory budget storage (non-persistent)
        # Key: tenant_id (str), Value: TokenBudget
        self._budgets: dict[str, TokenBudget] = {}

        # Usage history for savings calculation
        # Key: tenant_id (str), Value: list of UsageRecord
        self._usage_history: dict[str, list[UsageRecord]] = {}

        log.info(
            "budget_manager.initialized",
            default_daily_limit=default_daily_limit,
            default_monthly_limit=default_monthly_limit,
        )

    def check_budget(self, tenant_id: uuid.UUID, estimated_tokens: int) -> bool:
        """Check if tenant can afford estimated token usage.

        Checks both daily and monthly limits. Automatically resets counters
        if new day/month has started.

        Args:
            tenant_id: Tenant UUID
            estimated_tokens: Estimated tokens for this request

        Returns:
            True if request can proceed, False if over budget
        """
        budget = self._get_or_create_budget(tenant_id)
        self._maybe_reset_counters(budget)

        # Check if request would exceed either limit
        daily_available = budget.daily_limit - budget.current_daily
        monthly_available = budget.monthly_limit - budget.current_monthly

        can_afford = (
            estimated_tokens <= daily_available and estimated_tokens <= monthly_available
        )

        if not can_afford:
            log.warning(
                "budget_manager.budget_exceeded",
                tenant_id=str(tenant_id),
                estimated_tokens=estimated_tokens,
                daily_remaining=daily_available,
                monthly_remaining=monthly_available,
            )
        else:
            log.debug(
                "budget_manager.budget_check_passed",
                tenant_id=str(tenant_id),
                estimated_tokens=estimated_tokens,
                daily_remaining=daily_available,
                monthly_remaining=monthly_available,
            )

        return can_afford

    def record_usage(
        self,
        tenant_id: uuid.UUID,
        model_tier: ModelTier,
        input_tokens: int,
        output_tokens: int,
        complexity_score: float = 0.0,
    ) -> None:
        """Record actual token usage for a request.

        Updates budget counters and usage history. Emits alerts if
        thresholds are crossed.

        Args:
            tenant_id: Tenant UUID
            model_tier: Model tier used (for savings calculation)
            input_tokens: Input tokens consumed
            output_tokens: Output tokens consumed
            complexity_score: Task complexity score (for savings analysis)
        """
        budget = self._get_or_create_budget(tenant_id)
        self._maybe_reset_counters(budget)

        total_tokens = input_tokens + output_tokens

        # Update counters
        budget.current_daily += total_tokens
        budget.current_monthly += total_tokens

        # Record usage history
        record = UsageRecord(
            timestamp=datetime.now(UTC),
            model_tier=model_tier.value,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            complexity_score=complexity_score,
        )

        tenant_key = str(tenant_id)
        if tenant_key not in self._usage_history:
            self._usage_history[tenant_key] = []
        self._usage_history[tenant_key].append(record)

        log.info(
            "budget_manager.usage_recorded",
            tenant_id=str(tenant_id),
            model_tier=model_tier.value,
            total_tokens=total_tokens,
            daily_used=budget.current_daily,
            daily_limit=budget.daily_limit,
            monthly_used=budget.current_monthly,
            monthly_limit=budget.monthly_limit,
        )

        # Check thresholds and emit alerts
        self._check_thresholds(tenant_id, budget)

    def get_usage(self, tenant_id: uuid.UUID) -> TokenBudget:
        """Get current budget and usage for a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            TokenBudget with current usage
        """
        budget = self._get_or_create_budget(tenant_id)
        self._maybe_reset_counters(budget)
        return budget

    def get_savings_report(self, tenant_id: uuid.UUID) -> dict[str, int | float]:
        """Calculate savings from intelligent routing.

        Estimates how many tokens would have been used if all requests
        went to HEAVY tier, compared to actual usage with routing.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Dict with savings metrics:
            - tokens_saved: Tokens saved by routing
            - cost_reduction_pct: Percentage cost reduction
            - heavy_tier_count: Requests routed to HEAVY
            - standard_tier_count: Requests routed to STANDARD
            - light_tier_count: Requests routed to LIGHT
        """
        tenant_key = str(tenant_id)
        history = self._usage_history.get(tenant_key, [])

        if not history:
            return {
                "tokens_saved": 0,
                "cost_reduction_pct": 0.0,
                "heavy_tier_count": 0,
                "standard_tier_count": 0,
                "light_tier_count": 0,
            }

        # Count tier distribution
        tier_counts = {"light": 0, "standard": 0, "heavy": 0}
        total_tokens_actual = 0

        # Cost weights (relative to LIGHT=1.0)
        cost_weights = {"light": 1.0, "standard": 3.0, "heavy": 10.0}

        actual_cost = 0.0

        for record in history:
            tier_counts[record.model_tier] += 1
            total_tokens_actual += record.total_tokens
            actual_cost += record.total_tokens * cost_weights[record.model_tier]

        # Estimate cost if everything went to HEAVY
        heavy_cost = total_tokens_actual * cost_weights["heavy"]

        # Calculate savings
        tokens_saved_estimate = int((heavy_cost - actual_cost) / cost_weights["heavy"])
        cost_reduction_pct = (
            ((heavy_cost - actual_cost) / heavy_cost * 100.0) if heavy_cost > 0 else 0.0
        )

        report = {
            "tokens_saved": tokens_saved_estimate,
            "cost_reduction_pct": round(cost_reduction_pct, 2),
            "heavy_tier_count": tier_counts["heavy"],
            "standard_tier_count": tier_counts["standard"],
            "light_tier_count": tier_counts["light"],
        }

        log.info(
            "budget_manager.savings_report",
            tenant_id=str(tenant_id),
            **report,
        )

        return report

    def _get_or_create_budget(self, tenant_id: uuid.UUID) -> TokenBudget:
        """Get existing budget or create with defaults."""
        tenant_key = str(tenant_id)

        if tenant_key not in self._budgets:
            self._budgets[tenant_key] = TokenBudget(
                tenant_id=tenant_id,
                daily_limit=self._default_daily,
                monthly_limit=self._default_monthly,
            )
            log.debug(
                "budget_manager.budget_created",
                tenant_id=str(tenant_id),
                daily_limit=self._default_daily,
                monthly_limit=self._default_monthly,
            )

        return self._budgets[tenant_key]

    def _maybe_reset_counters(self, budget: TokenBudget) -> None:
        """Reset daily/monthly counters if new period has started."""
        now = datetime.now(UTC)
        current_date = now.strftime("%Y-%m-%d")
        current_month = now.strftime("%Y-%m")

        # Reset daily counter
        if current_date != budget.last_reset_date:
            log.info(
                "budget_manager.daily_reset",
                tenant_id=str(budget.tenant_id),
                previous_usage=budget.current_daily,
            )
            budget.current_daily = 0
            budget.last_reset_date = current_date

        # Reset monthly counter
        if current_month != budget.last_reset_month:
            log.info(
                "budget_manager.monthly_reset",
                tenant_id=str(budget.tenant_id),
                previous_usage=budget.current_monthly,
            )
            budget.current_monthly = 0
            budget.last_reset_month = current_month

    def _check_thresholds(self, tenant_id: uuid.UUID, budget: TokenBudget) -> None:
        """Check usage against alert thresholds and log warnings."""
        # Check daily usage
        daily_pct = budget.current_daily / budget.daily_limit

        if daily_pct >= self.CRITICAL_THRESHOLD:
            log.critical(
                "budget_manager.daily_critical",
                tenant_id=str(tenant_id),
                usage_pct=round(daily_pct * 100, 1),
                used=budget.current_daily,
                limit=budget.daily_limit,
            )
        elif daily_pct >= self.WARNING_THRESHOLD:
            log.warning(
                "budget_manager.daily_warning",
                tenant_id=str(tenant_id),
                usage_pct=round(daily_pct * 100, 1),
                used=budget.current_daily,
                limit=budget.daily_limit,
            )

        # Check monthly usage
        monthly_pct = budget.current_monthly / budget.monthly_limit

        if monthly_pct >= self.CRITICAL_THRESHOLD:
            log.critical(
                "budget_manager.monthly_critical",
                tenant_id=str(tenant_id),
                usage_pct=round(monthly_pct * 100, 1),
                used=budget.current_monthly,
                limit=budget.monthly_limit,
            )
        elif monthly_pct >= self.WARNING_THRESHOLD:
            log.warning(
                "budget_manager.monthly_warning",
                tenant_id=str(tenant_id),
                usage_pct=round(monthly_pct * 100, 1),
                used=budget.current_monthly,
                limit=budget.monthly_limit,
            )


# ---------------------------------------------------------------------------
# Persistent budget manager backed by PostgreSQL
# ---------------------------------------------------------------------------


class PersistentBudgetManager(BudgetManager):
    """Token budget manager backed by PostgreSQL.

    Extends BudgetManager with async database persistence so that budgets
    survive process restarts and remain correct under multiple workers.

    All mutating operations use SELECT ... FOR UPDATE on the token_budgets
    row to serialise concurrent updates within a single budget period.
    Read-only operations (check_budget, get_usage) use a regular SELECT so
    they do not block writers unnecessarily; callers that need a fully
    consistent check before recording should call check_budget and
    record_usage inside the same database transaction.

    The session factory is injected so the caller controls transaction
    scope and connection pooling.

    Usage:
        session_factory = async_sessionmaker(engine, ...)
        manager = PersistentBudgetManager(session_factory)

        # Non-async public API — delegates to async helpers via caller's event loop
        async def handle_request(tenant_id, tokens):
            if await manager.async_check_budget(session, tenant_id, tokens):
                await manager.async_record_usage(session, tenant_id, tier, in, out)
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        default_daily_limit: int = 1_000_000,
        default_monthly_limit: int = 20_000_000,
    ) -> None:
        """Initialize persistent budget manager.

        Args:
            session_factory: Callable that returns an AsyncSession. Typically
                the async_sessionmaker from src.database.
            default_daily_limit: Default daily token limit for new tenants
            default_monthly_limit: Default monthly token limit for new tenants
        """
        super().__init__(
            default_daily_limit=default_daily_limit,
            default_monthly_limit=default_monthly_limit,
        )
        self._session_factory = session_factory

        log.info(
            "persistent_budget_manager.initialized",
            default_daily_limit=default_daily_limit,
            default_monthly_limit=default_monthly_limit,
        )

    # ------------------------------------------------------------------
    # Async public API  (preferred — use these from async callers)
    # ------------------------------------------------------------------

    async def async_check_budget(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        estimated_tokens: int,
    ) -> bool:
        """Check if tenant can afford estimated token usage (async, DB-backed).

        Reads the current budget from the database, resets counters if
        needed, and returns True if the request can proceed.

        Does NOT acquire a row lock — use only for advisory checks before
        recording usage with async_record_usage.

        Args:
            session: Active async database session
            tenant_id: Tenant UUID
            estimated_tokens: Estimated tokens for this request

        Returns:
            True if request can proceed, False if over budget
        """
        record = await self._get_or_create_db_budget(session, tenant_id, lock=False)
        record = self._apply_resets(record)

        daily_available = record.daily_limit - record.current_daily
        monthly_available = record.monthly_limit - record.current_monthly

        can_afford = (
            estimated_tokens <= daily_available and estimated_tokens <= monthly_available
        )

        if not can_afford:
            log.warning(
                "persistent_budget_manager.budget_exceeded",
                tenant_id=str(tenant_id),
                estimated_tokens=estimated_tokens,
                daily_remaining=daily_available,
                monthly_remaining=monthly_available,
            )
        else:
            log.debug(
                "persistent_budget_manager.budget_check_passed",
                tenant_id=str(tenant_id),
                estimated_tokens=estimated_tokens,
                daily_remaining=daily_available,
                monthly_remaining=monthly_available,
            )

        return can_afford

    async def async_record_usage(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        model_tier: ModelTier,
        input_tokens: int,
        output_tokens: int,
        complexity_score: float = 0.0,
    ) -> None:
        """Record actual token usage (async, DB-backed).

        Uses SELECT ... FOR UPDATE to serialise concurrent updates on the
        budget row, then appends a TokenUsageRecord for the audit log.

        Both the budget update and the usage insert are performed within
        the caller-provided session so they commit atomically with the
        surrounding transaction.

        Args:
            session: Active async database session
            tenant_id: Tenant UUID
            model_tier: Model tier used
            input_tokens: Input tokens consumed
            output_tokens: Output tokens consumed
            complexity_score: Task complexity score
        """
        total_tokens = input_tokens + output_tokens

        # Lock the budget row for update to prevent lost writes
        budget_record = await self._get_or_create_db_budget(
            session, tenant_id, lock=True
        )
        budget_record = self._apply_resets(budget_record)

        # Update counters
        budget_record.current_daily += total_tokens
        budget_record.current_monthly += total_tokens
        budget_record.updated_at = datetime.now(UTC)

        # Append usage event
        usage = TokenUsageRecord(
            tenant_id=tenant_id,
            timestamp=datetime.now(UTC),
            model_tier=model_tier.value,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            complexity_score=complexity_score,
        )
        session.add(usage)

        log.info(
            "persistent_budget_manager.usage_recorded",
            tenant_id=str(tenant_id),
            model_tier=model_tier.value,
            total_tokens=total_tokens,
            daily_used=budget_record.current_daily,
            daily_limit=budget_record.daily_limit,
            monthly_used=budget_record.current_monthly,
            monthly_limit=budget_record.monthly_limit,
        )

        # Emit threshold alerts using the in-memory helper
        budget = TokenBudget(
            tenant_id=tenant_id,
            daily_limit=budget_record.daily_limit,
            monthly_limit=budget_record.monthly_limit,
            current_daily=budget_record.current_daily,
            current_monthly=budget_record.current_monthly,
        )
        self._check_thresholds(tenant_id, budget)

    async def async_get_usage(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> TokenBudget:
        """Get current budget and usage for a tenant from the database.

        Args:
            session: Active async database session
            tenant_id: Tenant UUID

        Returns:
            TokenBudget dataclass populated from DB record
        """
        record = await self._get_or_create_db_budget(session, tenant_id, lock=False)
        record = self._apply_resets(record)

        return TokenBudget(
            tenant_id=tenant_id,
            daily_limit=record.daily_limit,
            monthly_limit=record.monthly_limit,
            current_daily=record.current_daily,
            current_monthly=record.current_monthly,
            last_reset_date=record.last_reset_date,
            last_reset_month=record.last_reset_month,
        )

    async def async_get_savings_report(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> dict[str, int | float]:
        """Calculate savings from intelligent routing using DB usage history.

        Queries all TokenUsageRecord rows for the tenant and applies the
        same heavy-tier baseline calculation as the in-memory version.

        Args:
            session: Active async database session
            tenant_id: Tenant UUID

        Returns:
            Dict with savings metrics (same shape as BudgetManager.get_savings_report)
        """
        stmt = select(TokenUsageRecord).where(
            TokenUsageRecord.tenant_id == tenant_id
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

        if not rows:
            return {
                "tokens_saved": 0,
                "cost_reduction_pct": 0.0,
                "heavy_tier_count": 0,
                "standard_tier_count": 0,
                "light_tier_count": 0,
            }

        tier_counts: dict[str, int] = {"light": 0, "standard": 0, "heavy": 0}
        cost_weights = {"light": 1.0, "standard": 3.0, "heavy": 10.0}

        actual_cost = 0.0
        total_tokens_actual = 0

        for row in rows:
            tier = row.model_tier
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            total_tokens_actual += row.total_tokens
            actual_cost += row.total_tokens * cost_weights.get(tier, 1.0)

        heavy_cost = total_tokens_actual * cost_weights["heavy"]
        tokens_saved_estimate = int((heavy_cost - actual_cost) / cost_weights["heavy"])
        cost_reduction_pct = (
            ((heavy_cost - actual_cost) / heavy_cost * 100.0) if heavy_cost > 0 else 0.0
        )

        report: dict[str, int | float] = {
            "tokens_saved": tokens_saved_estimate,
            "cost_reduction_pct": round(cost_reduction_pct, 2),
            "heavy_tier_count": tier_counts.get("heavy", 0),
            "standard_tier_count": tier_counts.get("standard", 0),
            "light_tier_count": tier_counts.get("light", 0),
        }

        log.info(
            "persistent_budget_manager.savings_report",
            tenant_id=str(tenant_id),
            **report,
        )

        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_or_create_db_budget(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        lock: bool,
    ) -> TokenBudgetRecord:
        """Fetch the budget row for a tenant, creating it if absent.

        When lock=True, uses SELECT ... FOR UPDATE to serialise concurrent
        updates. When lock=False, a plain SELECT is used for read-only paths.

        Args:
            session: Active async database session
            tenant_id: Tenant UUID
            lock: Whether to acquire a row-level write lock

        Returns:
            TokenBudgetRecord ORM instance attached to the session
        """
        stmt = select(TokenBudgetRecord).where(
            TokenBudgetRecord.tenant_id == tenant_id
        )
        if lock:
            stmt = stmt.with_for_update()

        result = await session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            now = datetime.now(UTC)
            record = TokenBudgetRecord(
                tenant_id=tenant_id,
                daily_limit=self._default_daily,
                monthly_limit=self._default_monthly,
                current_daily=0,
                current_monthly=0,
                last_reset_date=now.strftime("%Y-%m-%d"),
                last_reset_month=now.strftime("%Y-%m"),
                updated_at=now,
            )
            session.add(record)
            await session.flush()  # Assign PK without committing
            log.debug(
                "persistent_budget_manager.budget_created",
                tenant_id=str(tenant_id),
                daily_limit=self._default_daily,
                monthly_limit=self._default_monthly,
            )

        return record

    @staticmethod
    def _apply_resets(record: TokenBudgetRecord) -> TokenBudgetRecord:
        """Reset daily/monthly counters on the DB record if periods have rolled over.

        Mutates record in-place (the ORM instance tracks the change for the
        session's unit-of-work). Only called inside a locked transaction so
        no concurrent writer can observe a partial reset.

        Args:
            record: TokenBudgetRecord ORM instance to check and possibly reset

        Returns:
            The same record (mutated in-place, returned for convenience)
        """
        now = datetime.now(UTC)
        current_date = now.strftime("%Y-%m-%d")
        current_month = now.strftime("%Y-%m")

        if current_date != record.last_reset_date:
            log.info(
                "persistent_budget_manager.daily_reset",
                tenant_id=str(record.tenant_id),
                previous_usage=record.current_daily,
            )
            record.current_daily = 0
            record.last_reset_date = current_date

        if current_month != record.last_reset_month:
            log.info(
                "persistent_budget_manager.monthly_reset",
                tenant_id=str(record.tenant_id),
                previous_usage=record.current_monthly,
            )
            record.current_monthly = 0
            record.last_reset_month = current_month

        return record
