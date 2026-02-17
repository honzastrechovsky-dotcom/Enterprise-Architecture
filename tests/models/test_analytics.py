"""Tests for analytics models.

These tests verify that analytics models properly track usage metrics
and daily summaries with correct indexes and constraints.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.analytics import DailySummary, MetricType, UsageMetric

# All tests require a real database (add, commit, query).
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_usage_metric_creation(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test creating a usage metric record."""
    metric = UsageMetric(
        tenant_id=test_tenant_id,
        metric_type=MetricType.API_CALL,
        value=1.0,
        dimensions={"endpoint": "/api/v1/chat", "method": "POST", "status_code": 200},
        timestamp=datetime.now(timezone.utc),
    )
    db_session.add(metric)
    await db_session.commit()

    # Verify it was saved
    stmt = select(UsageMetric).where(UsageMetric.tenant_id == test_tenant_id)
    result = await db_session.execute(stmt)
    saved = result.scalar_one()

    assert saved.metric_type == MetricType.API_CALL
    assert saved.value == 1.0
    assert saved.dimensions["endpoint"] == "/api/v1/chat"


@pytest.mark.asyncio
async def test_usage_metric_token_usage(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test recording token usage metrics."""
    metric = UsageMetric(
        tenant_id=test_tenant_id,
        metric_type=MetricType.TOKEN_USAGE,
        value=1250.0,
        dimensions={
            "model": "gpt-4o-mini",
            "prompt_tokens": 500,
            "completion_tokens": 750,
            "cost": 0.025,
        },
        timestamp=datetime.now(timezone.utc),
    )
    db_session.add(metric)
    await db_session.commit()

    stmt = select(UsageMetric).where(UsageMetric.metric_type == MetricType.TOKEN_USAGE)
    result = await db_session.execute(stmt)
    saved = result.scalar_one()

    assert saved.value == 1250.0
    assert saved.dimensions["model"] == "gpt-4o-mini"
    assert saved.dimensions["cost"] == 0.025


@pytest.mark.asyncio
async def test_daily_summary_creation(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test creating a daily summary record."""
    summary = DailySummary(
        tenant_id=test_tenant_id,
        date=date.today(),
        total_api_calls=1250,
        total_tokens=50000,
        total_agent_runs=45,
        unique_users=8,
        avg_response_time_ms=234.5,
        error_count=3,
        cost_estimate=12.50,
        dimensions={"models_used": ["gpt-4o-mini", "gpt-4o"], "peak_hour": 14},
    )
    db_session.add(summary)
    await db_session.commit()

    stmt = select(DailySummary).where(DailySummary.tenant_id == test_tenant_id)
    result = await db_session.execute(stmt)
    saved = result.scalar_one()

    assert saved.total_api_calls == 1250
    assert saved.total_tokens == 50000
    assert saved.unique_users == 8
    assert saved.cost_estimate == 12.50


@pytest.mark.asyncio
async def test_daily_summary_unique_constraint(
    db_session: AsyncSession, test_tenant_id: uuid.UUID
) -> None:
    """Test that tenant_id + date is unique (no duplicate daily summaries)."""
    today = date.today()
    summary1 = DailySummary(
        tenant_id=test_tenant_id,
        date=today,
        total_api_calls=100,
        total_tokens=1000,
        total_agent_runs=10,
        unique_users=2,
        avg_response_time_ms=100.0,
        error_count=0,
        cost_estimate=1.0,
    )
    db_session.add(summary1)
    await db_session.commit()

    # Try to add another summary for the same tenant and date
    summary2 = DailySummary(
        tenant_id=test_tenant_id,
        date=today,
        total_api_calls=200,
        total_tokens=2000,
        total_agent_runs=20,
        unique_users=3,
        avg_response_time_ms=150.0,
        error_count=1,
        cost_estimate=2.0,
    )
    db_session.add(summary2)

    with pytest.raises(Exception):  # IntegrityError or similar
        await db_session.commit()


@pytest.mark.asyncio
async def test_usage_metric_tenant_scoping(
    db_session: AsyncSession, test_tenant_id: uuid.UUID
) -> None:
    """Test that usage metrics are properly scoped by tenant."""
    other_tenant_id = uuid.uuid4()

    # Create metrics for two different tenants
    metric1 = UsageMetric(
        tenant_id=test_tenant_id,
        metric_type=MetricType.API_CALL,
        value=1.0,
        dimensions={"endpoint": "/api/v1/chat"},
        timestamp=datetime.now(timezone.utc),
    )
    metric2 = UsageMetric(
        tenant_id=other_tenant_id,
        metric_type=MetricType.API_CALL,
        value=1.0,
        dimensions={"endpoint": "/api/v1/documents"},
        timestamp=datetime.now(timezone.utc),
    )
    db_session.add_all([metric1, metric2])
    await db_session.commit()

    # Query for first tenant's metrics only
    stmt = select(UsageMetric).where(UsageMetric.tenant_id == test_tenant_id)
    result = await db_session.execute(stmt)
    metrics = result.scalars().all()

    assert len(metrics) == 1
    assert metrics[0].dimensions["endpoint"] == "/api/v1/chat"
