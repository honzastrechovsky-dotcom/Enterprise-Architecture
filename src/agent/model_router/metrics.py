"""Metrics collection for model routing decisions and performance.

The ModelMetricsCollector tracks routing decisions, tier distribution,
quality outcomes, and cost savings. This provides observability into
the routing system and enables data-driven optimization.

Metrics tracked:
- Routing decisions (timestamp, tier, complexity, quality, tokens, latency)
- Tier distribution over time
- Quality by tier
- Savings estimates

PersistentMetricsCollector extends ModelMetricsCollector to store decisions
in PostgreSQL via SQLAlchemy async sessions, making metrics durable across
restarts and queryable by the analytics pipeline.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.token_budget import RoutingDecisionRecord

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


@dataclass
class RoutingDecision:
    """Record of a single routing decision and its outcome.

    Attributes:
        timestamp: When the decision was made
        tenant_id: UUID of tenant making request
        task_type: Type of task routed
        selected_tier: Model tier selected
        estimated_complexity: Complexity score that drove selection
        actual_quality: Quality score of response (0.0-1.0, optional)
        tokens_used: Total tokens consumed
        latency_ms: Response latency in milliseconds
        metadata: Additional routing metadata
    """

    timestamp: datetime
    tenant_id: uuid.UUID
    task_type: str
    selected_tier: str
    estimated_complexity: float
    actual_quality: float | None = None
    tokens_used: int = 0
    latency_ms: float = 0.0
    metadata: dict[str, str | int | float] = field(default_factory=dict)


class ModelMetricsCollector:
    """Collects and aggregates routing metrics for observability.

    Stores routing decisions in-memory (non-persistent). Use PersistentMetricsCollector
    for durability, or integrate with Prometheus/InfluxDB for time-series storage.
    """

    def __init__(self) -> None:
        """Initialize metrics collector."""
        # In-memory storage
        # Key: tenant_id (str), Value: list of RoutingDecision
        self._decisions: dict[str, list[RoutingDecision]] = {}

        log.info("model_metrics_collector.initialized")

    def record_decision(self, decision: RoutingDecision) -> None:
        """Record a routing decision and its outcome.

        Args:
            decision: RoutingDecision to record
        """
        tenant_key = str(decision.tenant_id)

        if tenant_key not in self._decisions:
            self._decisions[tenant_key] = []

        self._decisions[tenant_key].append(decision)

        log.info(
            "model_metrics.decision_recorded",
            tenant_id=str(decision.tenant_id),
            task_type=decision.task_type,
            selected_tier=decision.selected_tier,
            complexity=decision.estimated_complexity,
            tokens=decision.tokens_used,
            latency_ms=decision.latency_ms,
        )

    def get_tier_distribution(
        self,
        tenant_id: uuid.UUID,
        period_hours: int = 24,
    ) -> dict[str, int]:
        """Get distribution of tier usage over a time period.

        Args:
            tenant_id: Tenant UUID
            period_hours: Time window in hours (default 24)

        Returns:
            Dict mapping tier name to request count
        """
        tenant_key = str(tenant_id)
        decisions = self._decisions.get(tenant_key, [])

        # Filter to time window
        cutoff = datetime.now(UTC) - timedelta(hours=period_hours)
        recent = [d for d in decisions if d.timestamp >= cutoff]

        # Count by tier
        distribution = {"light": 0, "standard": 0, "heavy": 0}
        for decision in recent:
            tier = decision.selected_tier
            if tier in distribution:
                distribution[tier] += 1

        log.debug(
            "model_metrics.tier_distribution",
            tenant_id=str(tenant_id),
            period_hours=period_hours,
            distribution=distribution,
            total_requests=len(recent),
        )

        return distribution

    def get_savings_estimate(
        self,
        tenant_id: uuid.UUID,
        period_hours: int = 24,
    ) -> dict[str, int | float]:
        """Estimate cost savings from intelligent routing.

        Compares actual costs (with routing) to estimated costs if all
        requests went to HEAVY tier.

        Args:
            tenant_id: Tenant UUID
            period_hours: Time window in hours (default 24)

        Returns:
            Dict with savings metrics:
            - tokens_saved: Tokens saved by routing to cheaper tiers
            - gpu_hours_saved: Estimated GPU hours saved
            - cost_reduction_pct: Percentage cost reduction
        """
        tenant_key = str(tenant_id)
        decisions = self._decisions.get(tenant_key, [])

        # Filter to time window
        cutoff = datetime.now(UTC) - timedelta(hours=period_hours)
        recent = [d for d in decisions if d.timestamp >= cutoff]

        if not recent:
            return {
                "tokens_saved": 0,
                "gpu_hours_saved": 0.0,
                "cost_reduction_pct": 0.0,
            }

        # Cost weights (relative to LIGHT=1.0)
        cost_weights = {"light": 1.0, "standard": 3.0, "heavy": 10.0}

        # GPU time weights (relative hours per 1K tokens)
        gpu_time_weights = {
            "light": 0.001,  # 7B models are fast
            "standard": 0.003,
            "heavy": 0.010,
        }

        actual_cost = 0.0
        actual_gpu_hours = 0.0
        total_tokens = 0

        for decision in recent:
            tokens = decision.tokens_used
            tier = decision.selected_tier

            if tier in cost_weights:
                actual_cost += tokens * cost_weights[tier]
                actual_gpu_hours += (tokens / 1000.0) * gpu_time_weights[tier]
                total_tokens += tokens

        # Estimate if all went to HEAVY
        heavy_cost = total_tokens * cost_weights["heavy"]
        heavy_gpu_hours = (total_tokens / 1000.0) * gpu_time_weights["heavy"]

        # Calculate savings
        tokens_saved_estimate = int((heavy_cost - actual_cost) / cost_weights["heavy"])
        gpu_hours_saved = heavy_gpu_hours - actual_gpu_hours
        cost_reduction_pct = (
            ((heavy_cost - actual_cost) / heavy_cost * 100.0) if heavy_cost > 0 else 0.0
        )

        savings = {
            "tokens_saved": tokens_saved_estimate,
            "gpu_hours_saved": round(gpu_hours_saved, 3),
            "cost_reduction_pct": round(cost_reduction_pct, 2),
        }

        log.info(
            "model_metrics.savings_estimate",
            tenant_id=str(tenant_id),
            period_hours=period_hours,
            **savings,
        )

        return savings

    def get_quality_by_tier(
        self,
        tenant_id: uuid.UUID,
        period_hours: int = 24,
    ) -> dict[str, float]:
        """Get average quality score by tier.

        Only includes decisions where actual_quality was recorded.

        Args:
            tenant_id: Tenant UUID
            period_hours: Time window in hours (default 24)

        Returns:
            Dict mapping tier to average quality score (0.0-1.0)
        """
        tenant_key = str(tenant_id)
        decisions = self._decisions.get(tenant_key, [])

        # Filter to time window and only decisions with quality scores
        cutoff = datetime.now(UTC) - timedelta(hours=period_hours)
        recent_with_quality = [
            d
            for d in decisions
            if d.timestamp >= cutoff and d.actual_quality is not None
        ]

        # Group by tier and calculate averages
        tier_scores: dict[str, list[float]] = {"light": [], "standard": [], "heavy": []}

        for decision in recent_with_quality:
            tier = decision.selected_tier
            if tier in tier_scores and decision.actual_quality is not None:
                tier_scores[tier].append(decision.actual_quality)

        # Calculate averages
        quality_by_tier = {}
        for tier, scores in tier_scores.items():
            if scores:
                quality_by_tier[tier] = round(sum(scores) / len(scores), 3)
            else:
                quality_by_tier[tier] = 0.0

        log.debug(
            "model_metrics.quality_by_tier",
            tenant_id=str(tenant_id),
            period_hours=period_hours,
            quality=quality_by_tier,
            sample_sizes={tier: len(scores) for tier, scores in tier_scores.items()},
        )

        return quality_by_tier

    def export_metrics(
        self,
        tenant_id: uuid.UUID | None = None,
        period_hours: int = 24,
    ) -> list[dict[str, str | int | float]]:
        """Export metrics in a format suitable for dashboards.

        Args:
            tenant_id: Optional tenant filter (None = all tenants)
            period_hours: Time window in hours

        Returns:
            List of metric dicts suitable for JSON export
        """
        cutoff = datetime.now(UTC) - timedelta(hours=period_hours)

        # Filter decisions
        if tenant_id:
            tenant_key = str(tenant_id)
            all_decisions = self._decisions.get(tenant_key, [])
        else:
            # All tenants
            all_decisions = []
            for decisions_list in self._decisions.values():
                all_decisions.extend(decisions_list)

        recent = [d for d in all_decisions if d.timestamp >= cutoff]

        # Convert to export format
        metrics = []
        for decision in recent:
            metric: dict[str, str | int | float] = {
                "timestamp": decision.timestamp.isoformat(),
                "tenant_id": str(decision.tenant_id),
                "task_type": decision.task_type,
                "selected_tier": decision.selected_tier,
                "complexity": decision.estimated_complexity,
                "tokens_used": decision.tokens_used,
                "latency_ms": decision.latency_ms,
            }

            if decision.actual_quality is not None:
                metric["quality"] = decision.actual_quality

            # Merge metadata
            metric.update(decision.metadata)

            metrics.append(metric)

        log.info(
            "model_metrics.export",
            tenant_id=str(tenant_id) if tenant_id else "all",
            period_hours=period_hours,
            metric_count=len(metrics),
        )

        return metrics

    def clear_history(self, tenant_id: uuid.UUID) -> None:
        """Clear metrics history for a tenant. Used for testing.

        Args:
            tenant_id: Tenant UUID
        """
        tenant_key = str(tenant_id)
        if tenant_key in self._decisions:
            del self._decisions[tenant_key]
            log.debug("model_metrics.history_cleared", tenant_id=str(tenant_id))


# ---------------------------------------------------------------------------
# Persistent metrics collector backed by PostgreSQL
# ---------------------------------------------------------------------------


class PersistentMetricsCollector(ModelMetricsCollector):
    """Routing metrics collector backed by PostgreSQL.

    Extends ModelMetricsCollector with async database persistence so that
    routing history survives process restarts and is queryable by the
    analytics pipeline.

    Decisions are appended as RoutingDecisionRecord rows. All read methods
    query the database directly, filtering by tenant_id and a UTC timestamp
    window so that per-process memory state is not required.

    The session factory is injected so the caller controls transaction scope.

    Usage:
        session_factory = async_sessionmaker(engine, ...)
        collector = PersistentMetricsCollector(session_factory)

        async def handle_request(session, decision):
            await collector.async_record_decision(session, decision)
    """

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        """Initialize persistent metrics collector.

        Args:
            session_factory: Callable that returns an AsyncSession. Typically
                the async_sessionmaker from src.database.
        """
        super().__init__()
        self._session_factory = session_factory

        log.info("persistent_metrics_collector.initialized")

    # ------------------------------------------------------------------
    # Async public API  (preferred — use these from async callers)
    # ------------------------------------------------------------------

    async def async_record_decision(
        self,
        session: AsyncSession,
        decision: RoutingDecision,
    ) -> None:
        """Persist a routing decision to the database.

        Inserts a RoutingDecisionRecord row within the caller's session.
        The metadata dict is serialised to JSON text for storage.

        Args:
            session: Active async database session
            decision: RoutingDecision to persist
        """
        metadata_json: str | None = None
        if decision.metadata:
            metadata_json = json.dumps(decision.metadata)

        record = RoutingDecisionRecord(
            tenant_id=decision.tenant_id,
            timestamp=decision.timestamp,
            task_type=decision.task_type,
            selected_tier=decision.selected_tier,
            estimated_complexity=decision.estimated_complexity,
            actual_quality=decision.actual_quality,
            tokens_used=decision.tokens_used,
            latency_ms=decision.latency_ms,
            metadata_json=metadata_json,
        )
        session.add(record)

        log.info(
            "persistent_metrics.decision_recorded",
            tenant_id=str(decision.tenant_id),
            task_type=decision.task_type,
            selected_tier=decision.selected_tier,
            complexity=decision.estimated_complexity,
            tokens=decision.tokens_used,
            latency_ms=decision.latency_ms,
        )

    async def async_get_tier_distribution(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        period_hours: int = 24,
    ) -> dict[str, int]:
        """Get tier distribution over a time window from the database.

        Args:
            session: Active async database session
            tenant_id: Tenant UUID
            period_hours: Look-back window in hours (default 24)

        Returns:
            Dict mapping tier name to request count
        """
        cutoff = datetime.now(UTC) - timedelta(hours=period_hours)
        stmt = (
            select(RoutingDecisionRecord)
            .where(
                RoutingDecisionRecord.tenant_id == tenant_id,
                RoutingDecisionRecord.timestamp >= cutoff,
            )
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

        distribution: dict[str, int] = {"light": 0, "standard": 0, "heavy": 0}
        for row in rows:
            tier = row.selected_tier
            if tier in distribution:
                distribution[tier] += 1

        log.debug(
            "persistent_metrics.tier_distribution",
            tenant_id=str(tenant_id),
            period_hours=period_hours,
            distribution=distribution,
            total_requests=len(rows),
        )

        return distribution

    async def async_get_savings_estimate(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        period_hours: int = 24,
    ) -> dict[str, int | float]:
        """Estimate cost savings from intelligent routing using DB records.

        Args:
            session: Active async database session
            tenant_id: Tenant UUID
            period_hours: Look-back window in hours (default 24)

        Returns:
            Dict with savings metrics (same shape as ModelMetricsCollector.get_savings_estimate)
        """
        cutoff = datetime.now(UTC) - timedelta(hours=period_hours)
        stmt = (
            select(RoutingDecisionRecord)
            .where(
                RoutingDecisionRecord.tenant_id == tenant_id,
                RoutingDecisionRecord.timestamp >= cutoff,
            )
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

        if not rows:
            return {
                "tokens_saved": 0,
                "gpu_hours_saved": 0.0,
                "cost_reduction_pct": 0.0,
            }

        cost_weights = {"light": 1.0, "standard": 3.0, "heavy": 10.0}
        gpu_time_weights = {"light": 0.001, "standard": 0.003, "heavy": 0.010}

        actual_cost = 0.0
        actual_gpu_hours = 0.0
        total_tokens = 0

        for row in rows:
            tokens = row.tokens_used
            tier = row.selected_tier
            if tier in cost_weights:
                actual_cost += tokens * cost_weights[tier]
                actual_gpu_hours += (tokens / 1000.0) * gpu_time_weights[tier]
                total_tokens += tokens

        heavy_cost = total_tokens * cost_weights["heavy"]
        heavy_gpu_hours = (total_tokens / 1000.0) * gpu_time_weights["heavy"]

        tokens_saved_estimate = int((heavy_cost - actual_cost) / cost_weights["heavy"])
        gpu_hours_saved = heavy_gpu_hours - actual_gpu_hours
        cost_reduction_pct = (
            ((heavy_cost - actual_cost) / heavy_cost * 100.0) if heavy_cost > 0 else 0.0
        )

        savings: dict[str, int | float] = {
            "tokens_saved": tokens_saved_estimate,
            "gpu_hours_saved": round(gpu_hours_saved, 3),
            "cost_reduction_pct": round(cost_reduction_pct, 2),
        }

        log.info(
            "persistent_metrics.savings_estimate",
            tenant_id=str(tenant_id),
            period_hours=period_hours,
            **savings,
        )

        return savings

    async def async_get_quality_by_tier(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        period_hours: int = 24,
    ) -> dict[str, float]:
        """Get average quality score by tier from the database.

        Only includes rows where actual_quality is not NULL.

        Args:
            session: Active async database session
            tenant_id: Tenant UUID
            period_hours: Look-back window in hours (default 24)

        Returns:
            Dict mapping tier to average quality score (0.0-1.0)
        """
        cutoff = datetime.now(UTC) - timedelta(hours=period_hours)
        stmt = (
            select(RoutingDecisionRecord)
            .where(
                RoutingDecisionRecord.tenant_id == tenant_id,
                RoutingDecisionRecord.timestamp >= cutoff,
                RoutingDecisionRecord.actual_quality.is_not(None),
            )
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

        tier_scores: dict[str, list[float]] = {"light": [], "standard": [], "heavy": []}
        for row in rows:
            tier = row.selected_tier
            if tier in tier_scores and row.actual_quality is not None:
                tier_scores[tier].append(row.actual_quality)

        quality_by_tier: dict[str, float] = {}
        for tier, scores in tier_scores.items():
            quality_by_tier[tier] = round(sum(scores) / len(scores), 3) if scores else 0.0

        log.debug(
            "persistent_metrics.quality_by_tier",
            tenant_id=str(tenant_id),
            period_hours=period_hours,
            quality=quality_by_tier,
            sample_sizes={tier: len(scores) for tier, scores in tier_scores.items()},
        )

        return quality_by_tier

    async def async_export_metrics(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID | None = None,
        period_hours: int = 24,
    ) -> list[dict[str, Any]]:
        """Export routing decisions from the database for dashboards.

        Args:
            session: Active async database session
            tenant_id: Optional tenant filter (None = all tenants)
            period_hours: Look-back window in hours

        Returns:
            List of metric dicts suitable for JSON export
        """
        cutoff = datetime.now(UTC) - timedelta(hours=period_hours)

        stmt = select(RoutingDecisionRecord).where(
            RoutingDecisionRecord.timestamp >= cutoff
        )
        if tenant_id is not None:
            stmt = stmt.where(RoutingDecisionRecord.tenant_id == tenant_id)

        result = await session.execute(stmt)
        rows = result.scalars().all()

        metrics: list[dict[str, Any]] = []
        for row in rows:
            metric: dict[str, Any] = {
                "timestamp": row.timestamp.isoformat(),
                "tenant_id": str(row.tenant_id),
                "task_type": row.task_type,
                "selected_tier": row.selected_tier,
                "complexity": row.estimated_complexity,
                "tokens_used": row.tokens_used,
                "latency_ms": row.latency_ms,
            }

            if row.actual_quality is not None:
                metric["quality"] = row.actual_quality

            if row.metadata_json:
                try:
                    metric.update(json.loads(row.metadata_json))
                except json.JSONDecodeError:
                    log.warning(
                        "persistent_metrics.metadata_decode_error",
                        record_id=str(row.id),
                    )

            metrics.append(metric)

        log.info(
            "persistent_metrics.export",
            tenant_id=str(tenant_id) if tenant_id else "all",
            period_hours=period_hours,
            metric_count=len(metrics),
        )

        return metrics
