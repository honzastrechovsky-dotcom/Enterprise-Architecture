"""Tests for analytics service.

These tests verify the analytics query and aggregation logic.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.analytics import DailySummary, MetricType, UsageMetric
from src.services.analytics import AnalyticsService

# All tests require a real database (add, commit, query).
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_get_usage_summary(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test getting usage summary for a date range."""
    # Create metrics spanning 3 days
    base_date = datetime.now(timezone.utc) - timedelta(days=2)

    for day in range(3):
        for _ in range(10):  # 10 API calls per day
            metric = UsageMetric(
                tenant_id=test_tenant_id,
                metric_type=MetricType.API_CALL,
                value=1.0,
                dimensions={"endpoint": "/api/v1/chat", "status_code": 200},
                timestamp=base_date + timedelta(days=day, hours=1),
            )
            db_session.add(metric)

    await db_session.commit()

    service = AnalyticsService(db_session)
    date_from = (base_date - timedelta(days=1)).date()
    date_to = (base_date + timedelta(days=3)).date()

    summary = await service.get_usage_summary(test_tenant_id, date_from, date_to)

    assert summary.total_api_calls == 30  # 10 per day × 3 days
    assert summary.date_from == date_from
    assert summary.date_to == date_to


@pytest.mark.asyncio
async def test_get_token_usage_by_model(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test getting token usage broken down by model."""
    now = datetime.now(timezone.utc)

    # Create token metrics for different models
    models_data = [
        ("gpt-4o-mini", 1000, 500),
        ("gpt-4o-mini", 800, 400),
        ("gpt-4o", 2000, 1500),
    ]

    for model, prompt, completion in models_data:
        metric = UsageMetric(
            tenant_id=test_tenant_id,
            metric_type=MetricType.TOKEN_USAGE,
            value=float(prompt + completion),
            dimensions={
                "model": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cost": 0.01,
            },
            timestamp=now,
        )
        db_session.add(metric)

    await db_session.commit()

    service = AnalyticsService(db_session)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    usage_by_model = await service.get_token_usage_by_model(test_tenant_id, date_from, date_to)

    assert len(usage_by_model) == 2  # Two unique models

    # Find gpt-4o-mini entry
    mini_entry = next(m for m in usage_by_model if m.model == "gpt-4o-mini")
    assert mini_entry.total_tokens == 2700  # 1500 + 1200
    assert mini_entry.prompt_tokens == 1800
    assert mini_entry.completion_tokens == 900

    # Find gpt-4o entry
    gpt4_entry = next(m for m in usage_by_model if m.model == "gpt-4o")
    assert gpt4_entry.total_tokens == 3500
    assert gpt4_entry.prompt_tokens == 2000
    assert gpt4_entry.completion_tokens == 1500


@pytest.mark.asyncio
async def test_get_agent_performance(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test getting agent performance metrics."""
    now = datetime.now(timezone.utc)
    agent1 = uuid.uuid4()
    agent2 = uuid.uuid4()

    # Create agent run metrics
    agent_runs = [
        (agent1, 1000, 5, "success"),
        (agent1, 1500, 7, "success"),
        (agent1, 800, 4, "error"),
        (agent2, 2000, 10, "success"),
    ]

    for agent_id, duration, steps, status in agent_runs:
        metric = UsageMetric(
            tenant_id=test_tenant_id,
            metric_type=MetricType.AGENT_RUN,
            value=1.0,
            dimensions={
                "agent_id": str(agent_id),
                "duration_ms": duration,
                "steps": steps,
                "status": status,
            },
            timestamp=now,
        )
        db_session.add(metric)

    await db_session.commit()

    service = AnalyticsService(db_session)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    performance = await service.get_agent_performance(test_tenant_id, date_from, date_to)

    assert len(performance) == 2

    agent1_perf = next(p for p in performance if p.agent_id == str(agent1))
    assert agent1_perf.total_runs == 3
    assert agent1_perf.success_rate == pytest.approx(66.67, rel=0.1)  # 2 success, 1 error
    assert agent1_perf.avg_duration_ms == pytest.approx(1100.0, rel=1)  # (1000+1500+800)/3


@pytest.mark.asyncio
async def test_get_top_users(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test getting top users by activity."""
    now = datetime.now(timezone.utc)
    user1 = uuid.uuid4()
    user2 = uuid.uuid4()
    user3 = uuid.uuid4()

    # Create API call metrics for different users
    user_calls = [(user1, 50), (user2, 30), (user3, 20)]

    for user_id, call_count in user_calls:
        for _ in range(call_count):
            metric = UsageMetric(
                tenant_id=test_tenant_id,
                metric_type=MetricType.API_CALL,
                value=1.0,
                dimensions={"user_id": str(user_id), "endpoint": "/api/v1/chat"},
                timestamp=now,
            )
            db_session.add(metric)

    await db_session.commit()

    service = AnalyticsService(db_session)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    top_users = await service.get_top_users(test_tenant_id, date_from, date_to, limit=2)

    assert len(top_users) == 2  # Requested top 2
    assert top_users[0].user_id == str(user1)
    assert top_users[0].api_calls == 50
    assert top_users[1].user_id == str(user2)
    assert top_users[1].api_calls == 30


@pytest.mark.asyncio
async def test_get_cost_breakdown(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test getting cost breakdown."""
    now = datetime.now(timezone.utc)

    # Create token usage with costs
    costs_data = [
        ("gpt-4o-mini", 1000, 0.02),
        ("gpt-4o-mini", 1500, 0.03),
        ("gpt-4o", 2000, 0.50),
        ("gpt-4o", 3000, 0.75),
    ]

    for model, tokens, cost in costs_data:
        metric = UsageMetric(
            tenant_id=test_tenant_id,
            metric_type=MetricType.TOKEN_USAGE,
            value=float(tokens),
            dimensions={"model": model, "cost": cost},
            timestamp=now,
        )
        db_session.add(metric)

    await db_session.commit()

    service = AnalyticsService(db_session)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    breakdown = await service.get_cost_breakdown(test_tenant_id, date_from, date_to)

    assert breakdown.total_cost == pytest.approx(1.30, rel=0.01)  # 0.02+0.03+0.50+0.75
    assert len(breakdown.by_model) == 2

    mini_cost = next(c for c in breakdown.by_model if c.model == "gpt-4o-mini")
    assert mini_cost.cost == pytest.approx(0.05, rel=0.01)

    gpt4_cost = next(c for c in breakdown.by_model if c.model == "gpt-4o")
    assert gpt4_cost.cost == pytest.approx(1.25, rel=0.01)


@pytest.mark.asyncio
async def test_get_error_rate(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test calculating error rate."""
    now = datetime.now(timezone.utc)

    # Create API calls with various status codes
    status_codes = [200] * 90 + [500] * 5 + [404] * 5

    for status_code in status_codes:
        metric = UsageMetric(
            tenant_id=test_tenant_id,
            metric_type=MetricType.API_CALL,
            value=1.0,
            dimensions={"status_code": status_code, "endpoint": "/api/v1/test"},
            timestamp=now,
        )
        db_session.add(metric)

    await db_session.commit()

    service = AnalyticsService(db_session)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    error_rate = await service.get_error_rate(test_tenant_id, date_from, date_to)

    assert error_rate.total_requests == 100
    assert error_rate.error_count == 10  # 5xx and 4xx
    assert error_rate.error_rate == pytest.approx(10.0, rel=0.1)


@pytest.mark.asyncio
async def test_get_daily_trends(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test getting daily trend data."""
    base_date = date.today() - timedelta(days=5)

    # Create daily summaries for 5 days
    for day_offset in range(5):
        summary = DailySummary(
            tenant_id=test_tenant_id,
            date=base_date + timedelta(days=day_offset),
            total_api_calls=100 + (day_offset * 10),
            total_tokens=5000 + (day_offset * 500),
            total_agent_runs=20 + (day_offset * 2),
            unique_users=5 + day_offset,
            avg_response_time_ms=200.0 + (day_offset * 10),
            error_count=2 + day_offset,
            cost_estimate=10.0 + (day_offset * 1.5),
        )
        db_session.add(summary)

    await db_session.commit()

    service = AnalyticsService(db_session)
    trends = await service.get_daily_trends(test_tenant_id, days=5)

    assert len(trends) == 5
    assert trends[0].date == base_date
    assert trends[-1].date == base_date + timedelta(days=4)

    # Verify increasing trend
    assert trends[0].total_api_calls == 100
    assert trends[-1].total_api_calls == 140


@pytest.mark.asyncio
async def test_generate_daily_summary(db_session: AsyncSession, test_tenant_id: uuid.UUID) -> None:
    """Test generating a daily summary from raw metrics."""
    target_date = date.today()
    target_datetime = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=timezone.utc)

    # Create various metrics for today
    user1, user2 = uuid.uuid4(), uuid.uuid4()

    # API calls
    for _ in range(100):
        metric = UsageMetric(
            tenant_id=test_tenant_id,
            metric_type=MetricType.API_CALL,
            value=1.0,
            dimensions={"user_id": str(user1), "status_code": 200, "response_time_ms": 150},
            timestamp=target_datetime + timedelta(hours=2),
        )
        db_session.add(metric)

    # Token usage
    metric = UsageMetric(
        tenant_id=test_tenant_id,
        metric_type=MetricType.TOKEN_USAGE,
        value=10000.0,
        dimensions={"model": "gpt-4o-mini", "cost": 0.50},
        timestamp=target_datetime + timedelta(hours=3),
    )
    db_session.add(metric)

    # Agent runs
    for _ in range(25):
        metric = UsageMetric(
            tenant_id=test_tenant_id,
            metric_type=MetricType.AGENT_RUN,
            value=1.0,
            dimensions={"user_id": str(user2), "agent_id": str(uuid.uuid4()), "status": "success"},
            timestamp=target_datetime + timedelta(hours=4),
        )
        db_session.add(metric)

    await db_session.commit()

    service = AnalyticsService(db_session)
    summary = await service.generate_daily_summary(test_tenant_id, target_date)

    assert summary.date == target_date
    assert summary.total_api_calls == 100
    assert summary.total_tokens == 10000
    assert summary.total_agent_runs == 25
    assert summary.unique_users == 2  # user1 and user2
    assert summary.cost_estimate == pytest.approx(0.50, rel=0.01)
    assert summary.error_count == 0


@pytest.mark.asyncio
async def test_tenant_isolation(db_session: AsyncSession) -> None:
    """Test that analytics data is properly isolated between tenants."""
    tenant1 = uuid.uuid4()
    tenant2 = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Create metrics for tenant1
    for _ in range(50):
        metric = UsageMetric(
            tenant_id=tenant1,
            metric_type=MetricType.API_CALL,
            value=1.0,
            dimensions={"endpoint": "/api/v1/chat"},
            timestamp=now,
        )
        db_session.add(metric)

    # Create metrics for tenant2
    for _ in range(30):
        metric = UsageMetric(
            tenant_id=tenant2,
            metric_type=MetricType.API_CALL,
            value=1.0,
            dimensions={"endpoint": "/api/v1/documents"},
            timestamp=now,
        )
        db_session.add(metric)

    await db_session.commit()

    service = AnalyticsService(db_session)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    # Tenant1 should only see their metrics
    summary1 = await service.get_usage_summary(tenant1, date_from, date_to)
    assert summary1.total_api_calls == 50

    # Tenant2 should only see their metrics
    summary2 = await service.get_usage_summary(tenant2, date_from, date_to)
    assert summary2.total_api_calls == 30
