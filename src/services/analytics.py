"""Analytics service for querying and aggregating usage metrics.

Provides high-level analytics queries that power the dashboard:
- Usage summaries
- Token usage by model
- Agent performance metrics
- Top users
- Cost breakdown
- Error rates
- Daily trends

All queries are tenant-scoped for multi-tenancy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.analytics import DailySummary, MetricType, UsageMetric

log = structlog.get_logger(__name__)


# Response models
class UsageSummary(BaseModel):
    """Overall usage summary for a date range."""

    date_from: date
    date_to: date
    total_api_calls: int
    total_tokens: int
    total_agent_runs: int
    unique_users: int
    avg_response_time_ms: float
    error_count: int
    cost_estimate: float


class ModelUsage(BaseModel):
    """Token usage for a specific model."""

    model: str
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    api_calls: int
    cost: float


class AgentPerformance(BaseModel):
    """Performance metrics for an agent."""

    agent_id: str
    total_runs: int
    success_rate: float
    avg_duration_ms: float
    avg_steps: float


class UserUsage(BaseModel):
    """Usage metrics for a user."""

    user_id: str
    api_calls: int
    tokens: int
    agent_runs: int


class ModelCost(BaseModel):
    """Cost for a specific model."""

    model: str
    cost: float


class CostBreakdown(BaseModel):
    """Cost breakdown by model."""

    total_cost: float
    by_model: list[ModelCost]


class ErrorRate(BaseModel):
    """Error rate metrics."""

    total_requests: int
    error_count: int
    error_rate: float


class DailyTrend(BaseModel):
    """Daily trend data point."""

    date: date
    total_api_calls: int
    total_tokens: int
    total_agent_runs: int
    unique_users: int
    cost_estimate: float


class AnalyticsService:
    """Service for analytics queries and aggregations."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize analytics service.

        Args:
            db: Database session
        """
        self.db = db

    async def get_usage_summary(
        self,
        tenant_id: uuid.UUID,
        date_from: date,
        date_to: date,
    ) -> UsageSummary:
        """Get overall usage summary for a date range.

        Args:
            tenant_id: Tenant ID
            date_from: Start date (inclusive)
            date_to: End date (inclusive)

        Returns:
            Usage summary
        """
        start_dt = datetime.combine(date_from, datetime.min.time()).replace(tzinfo=UTC)
        end_dt = datetime.combine(date_to, datetime.max.time()).replace(tzinfo=UTC)

        # Count API calls
        api_call_stmt = select(func.count()).select_from(UsageMetric).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.API_CALL,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        api_calls_result = await self.db.execute(api_call_stmt)
        total_api_calls = api_calls_result.scalar() or 0

        # Sum tokens
        token_stmt = select(func.sum(UsageMetric.value)).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        tokens_result = await self.db.execute(token_stmt)
        total_tokens = int(tokens_result.scalar() or 0)

        # Count agent runs
        agent_stmt = select(func.count()).select_from(UsageMetric).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.AGENT_RUN,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        agent_result = await self.db.execute(agent_stmt)
        total_agent_runs = agent_result.scalar() or 0

        # Count unique users (from dimensions)
        unique_users = 0
        # This is a simplified version; in production, you'd extract user_id from dimensions
        # For now, we'll estimate from agent runs with user_id
        user_stmt = select(UsageMetric.dimensions).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.API_CALL,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        user_result = await self.db.execute(user_stmt)
        user_ids = set()
        for (dims,) in user_result:
            if "user_id" in dims:
                user_ids.add(dims["user_id"])
        unique_users = len(user_ids)

        # Average response time
        avg_response_time_ms = 0.0
        response_time_stmt = select(UsageMetric.dimensions).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.API_CALL,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        response_times = []
        rt_result = await self.db.execute(response_time_stmt)
        for (dims,) in rt_result:
            if "response_time_ms" in dims:
                response_times.append(dims["response_time_ms"])
        if response_times:
            avg_response_time_ms = sum(response_times) / len(response_times)

        # Count errors (status_code >= 400)
        error_count = 0
        error_stmt = select(UsageMetric.dimensions).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.API_CALL,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        error_result = await self.db.execute(error_stmt)
        for (dims,) in error_result:
            if "status_code" in dims and dims["status_code"] >= 400:
                error_count += 1

        # Sum costs
        cost_stmt = select(UsageMetric.dimensions).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        cost_result = await self.db.execute(cost_stmt)
        total_cost = 0.0
        for (dims,) in cost_result:
            if "cost" in dims:
                total_cost += dims["cost"]

        return UsageSummary(
            date_from=date_from,
            date_to=date_to,
            total_api_calls=total_api_calls,
            total_tokens=total_tokens,
            total_agent_runs=total_agent_runs,
            unique_users=unique_users,
            avg_response_time_ms=avg_response_time_ms,
            error_count=error_count,
            cost_estimate=total_cost,
        )

    async def get_token_usage_by_model(
        self,
        tenant_id: uuid.UUID,
        date_from: date,
        date_to: date,
    ) -> list[ModelUsage]:
        """Get token usage broken down by model.

        Args:
            tenant_id: Tenant ID
            date_from: Start date
            date_to: End date

        Returns:
            List of model usage stats
        """
        start_dt = datetime.combine(date_from, datetime.min.time()).replace(tzinfo=UTC)
        end_dt = datetime.combine(date_to, datetime.max.time()).replace(tzinfo=UTC)

        stmt = select(UsageMetric).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        result = await self.db.execute(stmt)
        metrics = result.scalars().all()

        # Aggregate by model
        by_model: dict[str, dict[str, Any]] = {}
        for metric in metrics:
            model = metric.dimensions.get("model", "unknown")
            if model not in by_model:
                by_model[model] = {
                    "total_tokens": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "api_calls": 0,
                    "cost": 0.0,
                }

            by_model[model]["total_tokens"] += int(metric.value)
            by_model[model]["prompt_tokens"] += metric.dimensions.get("prompt_tokens", 0)
            by_model[model]["completion_tokens"] += metric.dimensions.get("completion_tokens", 0)
            by_model[model]["api_calls"] += 1
            by_model[model]["cost"] += metric.dimensions.get("cost", 0.0)

        return [
            ModelUsage(model=model, **stats)
            for model, stats in by_model.items()
        ]

    async def get_agent_performance(
        self,
        tenant_id: uuid.UUID,
        date_from: date,
        date_to: date,
    ) -> list[AgentPerformance]:
        """Get agent performance metrics.

        Args:
            tenant_id: Tenant ID
            date_from: Start date
            date_to: End date

        Returns:
            List of agent performance stats
        """
        start_dt = datetime.combine(date_from, datetime.min.time()).replace(tzinfo=UTC)
        end_dt = datetime.combine(date_to, datetime.max.time()).replace(tzinfo=UTC)

        stmt = select(UsageMetric).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.AGENT_RUN,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        result = await self.db.execute(stmt)
        metrics = result.scalars().all()

        # Aggregate by agent_id
        by_agent: dict[str, dict[str, Any]] = {}
        for metric in metrics:
            agent_id = metric.dimensions.get("agent_id", "unknown")
            if agent_id not in by_agent:
                by_agent[agent_id] = {
                    "total_runs": 0,
                    "successes": 0,
                    "durations": [],
                    "steps": [],
                }

            by_agent[agent_id]["total_runs"] += 1
            if metric.dimensions.get("status") == "success":
                by_agent[agent_id]["successes"] += 1
            by_agent[agent_id]["durations"].append(metric.dimensions.get("duration_ms", 0))
            by_agent[agent_id]["steps"].append(metric.dimensions.get("steps", 0))

        return [
            AgentPerformance(
                agent_id=agent_id,
                total_runs=stats["total_runs"],
                success_rate=(stats["successes"] / stats["total_runs"] * 100) if stats["total_runs"] > 0 else 0.0,
                avg_duration_ms=sum(stats["durations"]) / len(stats["durations"]) if stats["durations"] else 0.0,
                avg_steps=sum(stats["steps"]) / len(stats["steps"]) if stats["steps"] else 0.0,
            )
            for agent_id, stats in by_agent.items()
        ]

    async def get_top_users(
        self,
        tenant_id: uuid.UUID,
        date_from: date,
        date_to: date,
        limit: int = 10,
    ) -> list[UserUsage]:
        """Get top users by activity.

        Args:
            tenant_id: Tenant ID
            date_from: Start date
            date_to: End date
            limit: Maximum number of users to return

        Returns:
            List of user usage stats, ordered by activity
        """
        start_dt = datetime.combine(date_from, datetime.min.time()).replace(tzinfo=UTC)
        end_dt = datetime.combine(date_to, datetime.max.time()).replace(tzinfo=UTC)

        stmt = select(UsageMetric).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        result = await self.db.execute(stmt)
        metrics = result.scalars().all()

        # Aggregate by user_id
        by_user: dict[str, dict[str, int]] = {}
        for metric in metrics:
            user_id = metric.dimensions.get("user_id")
            if not user_id:
                continue

            if user_id not in by_user:
                by_user[user_id] = {"api_calls": 0, "tokens": 0, "agent_runs": 0}

            if metric.metric_type == MetricType.API_CALL:
                by_user[user_id]["api_calls"] += 1
            elif metric.metric_type == MetricType.TOKEN_USAGE:
                by_user[user_id]["tokens"] += int(metric.value)
            elif metric.metric_type == MetricType.AGENT_RUN:
                by_user[user_id]["agent_runs"] += 1

        # Sort by API calls and limit
        sorted_users = sorted(
            by_user.items(),
            key=lambda x: x[1]["api_calls"],
            reverse=True,
        )[:limit]

        return [
            UserUsage(user_id=user_id, **stats)
            for user_id, stats in sorted_users
        ]

    async def get_cost_breakdown(
        self,
        tenant_id: uuid.UUID,
        date_from: date,
        date_to: date,
    ) -> CostBreakdown:
        """Get cost breakdown by model.

        Args:
            tenant_id: Tenant ID
            date_from: Start date
            date_to: End date

        Returns:
            Cost breakdown
        """
        start_dt = datetime.combine(date_from, datetime.min.time()).replace(tzinfo=UTC)
        end_dt = datetime.combine(date_to, datetime.max.time()).replace(tzinfo=UTC)

        stmt = select(UsageMetric).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.TOKEN_USAGE,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        result = await self.db.execute(stmt)
        metrics = result.scalars().all()

        # Aggregate costs by model
        by_model: dict[str, float] = {}
        total_cost = 0.0

        for metric in metrics:
            model = metric.dimensions.get("model", "unknown")
            cost = metric.dimensions.get("cost", 0.0)
            by_model[model] = by_model.get(model, 0.0) + cost
            total_cost += cost

        return CostBreakdown(
            total_cost=total_cost,
            by_model=[
                ModelCost(model=model, cost=cost)
                for model, cost in by_model.items()
            ],
        )

    async def get_error_rate(
        self,
        tenant_id: uuid.UUID,
        date_from: date,
        date_to: date,
    ) -> ErrorRate:
        """Get error rate for API calls.

        Args:
            tenant_id: Tenant ID
            date_from: Start date
            date_to: End date

        Returns:
            Error rate stats
        """
        start_dt = datetime.combine(date_from, datetime.min.time()).replace(tzinfo=UTC)
        end_dt = datetime.combine(date_to, datetime.max.time()).replace(tzinfo=UTC)

        stmt = select(UsageMetric).where(
            and_(
                UsageMetric.tenant_id == tenant_id,
                UsageMetric.metric_type == MetricType.API_CALL,
                UsageMetric.timestamp >= start_dt,
                UsageMetric.timestamp <= end_dt,
            )
        )
        result = await self.db.execute(stmt)
        metrics = result.scalars().all()

        total_requests = len(metrics)
        error_count = 0

        for metric in metrics:
            status_code = metric.dimensions.get("status_code", 0)
            if status_code >= 400:
                error_count += 1

        error_rate = (error_count / total_requests * 100) if total_requests > 0 else 0.0

        return ErrorRate(
            total_requests=total_requests,
            error_count=error_count,
            error_rate=error_rate,
        )

    async def get_daily_trends(
        self,
        tenant_id: uuid.UUID,
        days: int = 30,
    ) -> list[DailyTrend]:
        """Get daily trend data from pre-aggregated summaries.

        Args:
            tenant_id: Tenant ID
            days: Number of days to retrieve

        Returns:
            List of daily trends, ordered by date
        """
        start_date = date.today() - timedelta(days=days - 1)

        stmt = select(DailySummary).where(
            and_(
                DailySummary.tenant_id == tenant_id,
                DailySummary.date >= start_date,
            )
        ).order_by(DailySummary.date)

        result = await self.db.execute(stmt)
        summaries = result.scalars().all()

        return [
            DailyTrend(
                date=summary.date,
                total_api_calls=summary.total_api_calls,
                total_tokens=summary.total_tokens,
                total_agent_runs=summary.total_agent_runs,
                unique_users=summary.unique_users,
                cost_estimate=summary.cost_estimate,
            )
            for summary in summaries
        ]

    async def generate_daily_summary(
        self,
        tenant_id: uuid.UUID,
        target_date: date,
    ) -> DailySummary:
        """Generate a daily summary by aggregating raw metrics.

        This is typically called by a scheduled job to pre-aggregate
        yesterday's metrics for faster dashboard queries.

        Args:
            tenant_id: Tenant ID
            target_date: Date to generate summary for

        Returns:
            Generated daily summary
        """
        # Get usage summary for the day
        summary = await self.get_usage_summary(tenant_id, target_date, target_date)

        # Create DailySummary record
        daily_summary = DailySummary(
            tenant_id=tenant_id,
            date=target_date,
            total_api_calls=summary.total_api_calls,
            total_tokens=summary.total_tokens,
            total_agent_runs=summary.total_agent_runs,
            unique_users=summary.unique_users,
            avg_response_time_ms=summary.avg_response_time_ms,
            error_count=summary.error_count,
            cost_estimate=summary.cost_estimate,
        )

        self.db.add(daily_summary)
        await self.db.commit()
        await self.db.refresh(daily_summary)

        log.info(
            "analytics.daily_summary_generated",
            tenant_id=str(tenant_id),
            date=str(target_date),
            api_calls=summary.total_api_calls,
        )

        return daily_summary
