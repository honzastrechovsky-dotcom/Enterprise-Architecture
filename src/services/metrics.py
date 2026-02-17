"""Metrics collector service.

Buffered, thread-safe metrics collection that periodically flushes to the database.
Uses singleton pattern to ensure single collector instance across the application.

Design:
- Internal buffer with asyncio.Lock for thread safety
- Auto-flush on buffer size (100 records) or time interval (10s)
- Non-blocking record methods for minimal performance impact
- Batch insert to DB for efficiency
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.analytics import MetricType, UsageMetric

log = structlog.get_logger(__name__)


class MetricsCollector:
    """Singleton metrics collector with buffered writes."""

    _instance: MetricsCollector | None = None
    _lock = asyncio.Lock()

    def __new__(cls) -> MetricsCollector:
        """Ensure singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize collector (only once due to singleton)."""
        if self._initialized:
            return

        self._buffer: list[UsageMetric] = []
        self._buffer_lock = asyncio.Lock()
        self._db_session: AsyncSession | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._initialized = True
        self._buffer_size_limit = 100
        self._flush_interval_seconds = 10

    async def initialize(self, db_session: AsyncSession) -> None:
        """Initialize with database session.

        Args:
            db_session: Database session for flushing metrics
        """
        self._db_session = db_session

        # Start background flush task if not already running
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._periodic_flush())

    async def _periodic_flush(self) -> None:
        """Background task that periodically flushes the buffer."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval_seconds)
                await self.flush()
            except asyncio.CancelledError:
                # Task cancelled during shutdown
                break
            except Exception as exc:
                log.error("metrics.periodic_flush_error", error=str(exc), exc_info=True)

    async def flush(self) -> None:
        """Flush buffered metrics to database."""
        if self._db_session is None:
            log.warning("metrics.flush_no_session", message="DB session not initialized")
            return

        async with self._buffer_lock:
            if not self._buffer:
                return

            # Take all metrics from buffer
            metrics_to_flush = self._buffer[:]
            self._buffer.clear()

        # Batch insert
        try:
            self._db_session.add_all(metrics_to_flush)
            await self._db_session.commit()
            log.info("metrics.flushed", count=len(metrics_to_flush))
        except Exception as exc:
            log.error(
                "metrics.flush_error",
                error=str(exc),
                count=len(metrics_to_flush),
                exc_info=True,
            )
            await self._db_session.rollback()

    async def _record_metric(
        self,
        tenant_id: uuid.UUID,
        metric_type: MetricType,
        value: float,
        dimensions: dict[str, Any],
    ) -> None:
        """Internal method to record a metric to the buffer.

        Args:
            tenant_id: Tenant ID for scoping
            metric_type: Type of metric
            value: Numeric value
            dimensions: Additional metric attributes
        """
        metric = UsageMetric(
            tenant_id=tenant_id,
            metric_type=metric_type,
            value=value,
            dimensions=dimensions,
            timestamp=datetime.now(UTC),
        )

        async with self._buffer_lock:
            self._buffer.append(metric)

            # Auto-flush if buffer is full
            if len(self._buffer) >= self._buffer_size_limit:
                # Schedule flush without blocking
                asyncio.create_task(self.flush())

    async def record_api_call(
        self,
        tenant_id: uuid.UUID,
        endpoint: str,
        method: str,
        status_code: int,
        response_time_ms: int,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Record an API call metric.

        Args:
            tenant_id: Tenant ID
            endpoint: API endpoint path
            method: HTTP method
            status_code: Response status code
            response_time_ms: Response time in milliseconds
            user_id: Optional user ID
        """
        dimensions = {
            "endpoint": endpoint,
            "method": method,
            "status_code": status_code,
            "response_time_ms": response_time_ms,
        }
        if user_id is not None:
            dimensions["user_id"] = str(user_id)

        await self._record_metric(
            tenant_id=tenant_id,
            metric_type=MetricType.API_CALL,
            value=1.0,
            dimensions=dimensions,
        )

    async def record_token_usage(
        self,
        tenant_id: uuid.UUID,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Record token usage metric.

        Args:
            tenant_id: Tenant ID
            model: LLM model identifier
            prompt_tokens: Number of prompt tokens
            completion_tokens: Number of completion tokens
            cost: Estimated cost in USD
            user_id: Optional user ID
        """
        dimensions = {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": cost,
        }
        if user_id is not None:
            dimensions["user_id"] = str(user_id)

        await self._record_metric(
            tenant_id=tenant_id,
            metric_type=MetricType.TOKEN_USAGE,
            value=float(prompt_tokens + completion_tokens),
            dimensions=dimensions,
        )

    async def record_agent_run(
        self,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        duration_ms: int,
        steps: int,
        status: str,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Record an agent run metric.

        Args:
            tenant_id: Tenant ID
            agent_id: Agent identifier
            duration_ms: Duration in milliseconds
            steps: Number of steps executed
            status: Final status (success, error, timeout, etc.)
            user_id: Optional user ID
        """
        dimensions = {
            "agent_id": str(agent_id),
            "duration_ms": duration_ms,
            "steps": steps,
            "status": status,
        }
        if user_id is not None:
            dimensions["user_id"] = str(user_id)

        await self._record_metric(
            tenant_id=tenant_id,
            metric_type=MetricType.AGENT_RUN,
            value=1.0,
            dimensions=dimensions,
        )

    async def record_tool_call(
        self,
        tenant_id: uuid.UUID,
        tool_name: str,
        duration_ms: int,
        success: bool,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Record a tool call metric.

        Args:
            tenant_id: Tenant ID
            tool_name: Name of the tool invoked
            duration_ms: Duration in milliseconds
            success: Whether the call succeeded
            user_id: Optional user ID
        """
        dimensions = {
            "tool_name": tool_name,
            "duration_ms": duration_ms,
            "success": success,
        }
        if user_id is not None:
            dimensions["user_id"] = str(user_id)

        await self._record_metric(
            tenant_id=tenant_id,
            metric_type=MetricType.TOOL_CALL,
            value=1.0,
            dimensions=dimensions,
        )

    async def record_document_query(
        self,
        tenant_id: uuid.UUID,
        query_type: str,
        result_count: int,
        duration_ms: int,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Record a document query metric.

        Args:
            tenant_id: Tenant ID
            query_type: Type of query (vector_search, keyword, etc.)
            result_count: Number of results returned
            duration_ms: Duration in milliseconds
            user_id: Optional user ID
        """
        dimensions = {
            "query_type": query_type,
            "result_count": result_count,
            "duration_ms": duration_ms,
        }
        if user_id is not None:
            dimensions["user_id"] = str(user_id)

        await self._record_metric(
            tenant_id=tenant_id,
            metric_type=MetricType.DOCUMENT_QUERY,
            value=1.0,
            dimensions=dimensions,
        )

    async def shutdown(self) -> None:
        """Shutdown the collector and flush remaining metrics."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final flush
        await self.flush()
