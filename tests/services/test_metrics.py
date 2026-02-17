"""Tests for metrics collector service.

These tests verify the buffered metrics collection system works correctly,
including thread-safety, automatic flushing, and proper DB persistence.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.analytics import MetricType, UsageMetric
from src.services.metrics import MetricsCollector

@pytest.mark.asyncio
async def test_metrics_collector_singleton() -> None:
    """Test that MetricsCollector is a singleton."""
    collector1 = MetricsCollector()
    collector2 = MetricsCollector()
    assert collector1 is collector2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_api_call(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test recording API call metrics."""
    collector = MetricsCollector()
    await collector.initialize(db_session)

    await collector.record_api_call(
        tenant_id=test_tenant_id,
        endpoint="/api/v1/chat",
        method="POST",
        status_code=200,
        response_time_ms=150,
    )

    # Force flush to DB
    await collector.flush()

    # Verify metric was saved
    stmt = select(UsageMetric).where(
        UsageMetric.tenant_id == test_tenant_id,
        UsageMetric.metric_type == MetricType.API_CALL,
    )
    result = await db_session.execute(stmt)
    metric = result.scalar_one()

    assert metric.value == 1.0
    assert metric.dimensions["endpoint"] == "/api/v1/chat"
    assert metric.dimensions["method"] == "POST"
    assert metric.dimensions["status_code"] == 200
    assert metric.dimensions["response_time_ms"] == 150


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_token_usage(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test recording token usage metrics."""
    collector = MetricsCollector()
    await collector.initialize(db_session)

    await collector.record_token_usage(
        tenant_id=test_tenant_id,
        model="gpt-4o-mini",
        prompt_tokens=500,
        completion_tokens=750,
        cost=0.025,
    )

    await collector.flush()

    stmt = select(UsageMetric).where(
        UsageMetric.tenant_id == test_tenant_id,
        UsageMetric.metric_type == MetricType.TOKEN_USAGE,
    )
    result = await db_session.execute(stmt)
    metric = result.scalar_one()

    assert metric.value == 1250.0  # total tokens
    assert metric.dimensions["model"] == "gpt-4o-mini"
    assert metric.dimensions["prompt_tokens"] == 500
    assert metric.dimensions["completion_tokens"] == 750
    assert metric.dimensions["cost"] == 0.025


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_agent_run(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test recording agent run metrics."""
    collector = MetricsCollector()
    await collector.initialize(db_session)

    agent_id = uuid.uuid4()
    await collector.record_agent_run(
        tenant_id=test_tenant_id,
        agent_id=agent_id,
        duration_ms=5000,
        steps=12,
        status="success",
    )

    await collector.flush()

    stmt = select(UsageMetric).where(
        UsageMetric.tenant_id == test_tenant_id,
        UsageMetric.metric_type == MetricType.AGENT_RUN,
    )
    result = await db_session.execute(stmt)
    metric = result.scalar_one()

    assert metric.value == 1.0
    assert metric.dimensions["agent_id"] == str(agent_id)
    assert metric.dimensions["duration_ms"] == 5000
    assert metric.dimensions["steps"] == 12
    assert metric.dimensions["status"] == "success"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_tool_call(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test recording tool call metrics."""
    collector = MetricsCollector()
    await collector.initialize(db_session)

    await collector.record_tool_call(
        tenant_id=test_tenant_id,
        tool_name="vector_search",
        duration_ms=45,
        success=True,
    )

    await collector.flush()

    stmt = select(UsageMetric).where(
        UsageMetric.tenant_id == test_tenant_id,
        UsageMetric.metric_type == MetricType.TOOL_CALL,
    )
    result = await db_session.execute(stmt)
    metric = result.scalar_one()

    assert metric.value == 1.0
    assert metric.dimensions["tool_name"] == "vector_search"
    assert metric.dimensions["duration_ms"] == 45
    assert metric.dimensions["success"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_document_query(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test recording document query metrics."""
    collector = MetricsCollector()
    await collector.initialize(db_session)

    await collector.record_document_query(
        tenant_id=test_tenant_id,
        query_type="vector_search",
        result_count=5,
        duration_ms=78,
    )

    await collector.flush()

    stmt = select(UsageMetric).where(
        UsageMetric.tenant_id == test_tenant_id,
        UsageMetric.metric_type == MetricType.DOCUMENT_QUERY,
    )
    result = await db_session.execute(stmt)
    metric = result.scalar_one()

    assert metric.value == 1.0
    assert metric.dimensions["query_type"] == "vector_search"
    assert metric.dimensions["result_count"] == 5
    assert metric.dimensions["duration_ms"] == 78


@pytest.mark.integration
@pytest.mark.asyncio
async def test_buffered_flush(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test that metrics are buffered and flushed in batches."""
    collector = MetricsCollector()
    await collector.initialize(db_session)

    # Record multiple metrics without manual flush
    for i in range(5):
        await collector.record_api_call(
            tenant_id=test_tenant_id,
            endpoint=f"/api/v1/endpoint{i}",
            method="GET",
            status_code=200,
            response_time_ms=100 + i,
        )

    # Metrics should be in buffer, not in DB yet
    stmt = select(UsageMetric).where(UsageMetric.tenant_id == test_tenant_id)
    result = await db_session.execute(stmt)
    metrics_before_flush = result.scalars().all()
    assert len(metrics_before_flush) == 0

    # Now flush
    await collector.flush()

    # All metrics should be in DB
    result = await db_session.execute(stmt)
    metrics_after_flush = result.scalars().all()
    assert len(metrics_after_flush) == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_thread_safety(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test that metrics collector is thread-safe with concurrent writes."""
    collector = MetricsCollector()
    await collector.initialize(db_session)

    async def record_metric(i: int) -> None:
        await collector.record_api_call(
            tenant_id=test_tenant_id,
            endpoint=f"/api/v1/test{i}",
            method="POST",
            status_code=200,
            response_time_ms=100,
        )

    # Record metrics concurrently
    await asyncio.gather(*[record_metric(i) for i in range(20)])

    await collector.flush()

    # All 20 metrics should be saved
    stmt = select(UsageMetric).where(UsageMetric.tenant_id == test_tenant_id)
    result = await db_session.execute(stmt)
    metrics = result.scalars().all()
    assert len(metrics) == 20


@pytest.mark.integration
@pytest.mark.asyncio
async def test_auto_flush_on_buffer_size(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test that buffer auto-flushes when it reaches 100 records."""
    collector = MetricsCollector()
    await collector.initialize(db_session)

    # Record exactly 100 metrics (should trigger auto-flush)
    for i in range(100):
        await collector.record_api_call(
            tenant_id=test_tenant_id,
            endpoint="/api/v1/test",
            method="GET",
            status_code=200,
            response_time_ms=100,
        )

    # Give it a moment for async flush to complete
    await asyncio.sleep(0.1)

    # Verify metrics are in DB (auto-flushed)
    stmt = select(UsageMetric).where(UsageMetric.tenant_id == test_tenant_id)
    result = await db_session.execute(stmt)
    metrics = result.scalars().all()
    assert len(metrics) >= 100  # May be more if flush happened


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multiple_tenants(db_session: AsyncSession) -> None:
    """Test that metrics from multiple tenants are tracked separately."""
    collector = MetricsCollector()
    await collector.initialize(db_session)

    tenant1 = uuid.uuid4()
    tenant2 = uuid.uuid4()

    await collector.record_api_call(
        tenant_id=tenant1, endpoint="/api/v1/chat", method="POST", status_code=200, response_time_ms=100
    )
    await collector.record_api_call(
        tenant_id=tenant2, endpoint="/api/v1/documents", method="GET", status_code=200, response_time_ms=150
    )

    await collector.flush()

    # Check tenant1's metrics
    stmt = select(UsageMetric).where(UsageMetric.tenant_id == tenant1)
    result = await db_session.execute(stmt)
    tenant1_metrics = result.scalars().all()
    assert len(tenant1_metrics) == 1
    assert tenant1_metrics[0].dimensions["endpoint"] == "/api/v1/chat"

    # Check tenant2's metrics
    stmt = select(UsageMetric).where(UsageMetric.tenant_id == tenant2)
    result = await db_session.execute(stmt)
    tenant2_metrics = result.scalars().all()
    assert len(tenant2_metrics) == 1
    assert tenant2_metrics[0].dimensions["endpoint"] == "/api/v1/documents"
