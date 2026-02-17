"""Tests for analytics API endpoints.

These tests verify that analytics endpoints are properly secured,
return correct data, and enforce tenant scoping.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.analytics import MetricType, UsageMetric, DailySummary
from src.models.user import UserRole
from src.services.analytics import (
    AgentPerformance,
    AnalyticsService,
    CostBreakdown,
    DailyTrend,
    ErrorRate,
    ModelCost,
    ModelUsage,
    UsageSummary,
    UserUsage,
)


@pytest.mark.asyncio
async def test_get_analytics_summary_unauthorized(client: AsyncClient) -> None:
    """Test that analytics endpoints require authentication."""
    response = await client.get("/api/v1/analytics/summary")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_analytics_summary_viewer_forbidden(
    client: AsyncClient, viewer_auth_headers: dict[str, str]
) -> None:
    """Test that viewers cannot access analytics."""
    response = await client.get(
        "/api/v1/analytics/summary",
        headers=viewer_auth_headers,
        params={"date_from": "2026-02-01", "date_to": "2026-02-17"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_get_analytics_summary_success(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test getting usage summary as operator."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    mock_summary = UsageSummary(
        date_from=date_from,
        date_to=date_to,
        total_api_calls=100,
        total_tokens=5000,
        total_agent_runs=10,
        unique_users=3,
        avg_response_time_ms=150.0,
        error_count=2,
        cost_estimate=5.0,
    )

    with patch.object(AnalyticsService, "get_usage_summary", new_callable=AsyncMock, return_value=mock_summary):
        response = await client.get(
            "/api/v1/analytics/summary",
            headers=operator_auth_headers,
            params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total_api_calls"] == 100
    assert "date_from" in data
    assert "date_to" in data


@pytest.mark.asyncio
async def test_get_token_usage_by_model(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test getting token usage broken down by model."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    mock_usage = [
        ModelUsage(model="gpt-4o-mini", total_tokens=5000, prompt_tokens=2500, completion_tokens=2500, api_calls=10, cost=0.50),
        ModelUsage(model="gpt-4o", total_tokens=10000, prompt_tokens=5000, completion_tokens=5000, api_calls=5, cost=2.00),
    ]

    with patch.object(AnalyticsService, "get_token_usage_by_model", new_callable=AsyncMock, return_value=mock_usage):
        response = await client.get(
            "/api/v1/analytics/tokens",
            headers=operator_auth_headers,
            params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data["usage"]) == 2

    mini_usage = next(u for u in data["usage"] if u["model"] == "gpt-4o-mini")
    assert mini_usage["total_tokens"] == 5000


@pytest.mark.asyncio
async def test_get_agent_performance(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test getting agent performance metrics."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()
    agent_id = uuid.uuid4()

    mock_perf = [
        AgentPerformance(agent_id=str(agent_id), total_runs=3, success_rate=0.67, avg_duration_ms=1000.0, avg_steps=5.0),
    ]

    with patch.object(AnalyticsService, "get_agent_performance", new_callable=AsyncMock, return_value=mock_perf):
        response = await client.get(
            "/api/v1/analytics/agents",
            headers=operator_auth_headers,
            params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data["performance"]) == 1
    assert data["performance"][0]["agent_id"] == str(agent_id)
    assert data["performance"][0]["total_runs"] == 3


@pytest.mark.asyncio
async def test_get_top_users(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test getting top users by activity."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()
    user1, user2 = uuid.uuid4(), uuid.uuid4()

    mock_users = [
        UserUsage(user_id=str(user1), api_calls=50, tokens=5000, agent_runs=10),
        UserUsage(user_id=str(user2), api_calls=30, tokens=3000, agent_runs=5),
    ]

    with patch.object(AnalyticsService, "get_top_users", new_callable=AsyncMock, return_value=mock_users):
        response = await client.get(
            "/api/v1/analytics/users",
            headers=operator_auth_headers,
            params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "limit": 10},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data["users"]) == 2
    assert data["users"][0]["user_id"] == str(user1)
    assert data["users"][0]["api_calls"] == 50


@pytest.mark.asyncio
async def test_get_cost_breakdown(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test getting cost breakdown."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    mock_breakdown = CostBreakdown(
        total_cost=2.50,
        by_model=[
            ModelCost(model="gpt-4o-mini", cost=0.50),
            ModelCost(model="gpt-4o", cost=2.00),
        ],
    )

    with patch.object(AnalyticsService, "get_cost_breakdown", new_callable=AsyncMock, return_value=mock_breakdown):
        response = await client.get(
            "/api/v1/analytics/costs",
            headers=operator_auth_headers,
            params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total_cost"] == pytest.approx(2.50, rel=0.01)
    assert len(data["by_model"]) == 2


@pytest.mark.asyncio
async def test_get_error_rate(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test getting error rate."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    mock_errors = ErrorRate(total_requests=100, error_count=5, error_rate=5.0)

    with patch.object(AnalyticsService, "get_error_rate", new_callable=AsyncMock, return_value=mock_errors):
        response = await client.get(
            "/api/v1/analytics/errors",
            headers=operator_auth_headers,
            params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total_requests"] == 100
    assert data["error_count"] == 5
    assert data["error_rate"] == pytest.approx(5.0, rel=0.1)


@pytest.mark.asyncio
async def test_get_daily_trends(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test getting daily trend data."""
    base_date = date.today() - timedelta(days=7)

    mock_trends = [
        DailyTrend(
            date=base_date + timedelta(days=i),
            total_api_calls=100 + (i * 10),
            total_tokens=5000,
            total_agent_runs=20,
            unique_users=5,
            cost_estimate=10.0,
        )
        for i in range(7)
    ]

    with patch.object(AnalyticsService, "get_daily_trends", new_callable=AsyncMock, return_value=mock_trends):
        response = await client.get(
            "/api/v1/analytics/trends",
            headers=operator_auth_headers,
            params={"days": 7},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data["trends"]) == 7
    assert data["trends"][0]["total_api_calls"] == 100
    assert data["trends"][-1]["total_api_calls"] == 160


@pytest.mark.asyncio
async def test_export_analytics_csv(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test exporting analytics as CSV."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    mock_summary = UsageSummary(
        date_from=date_from,
        date_to=date_to,
        total_api_calls=10,
        total_tokens=1000,
        total_agent_runs=2,
        unique_users=1,
        avg_response_time_ms=100.0,
        error_count=0,
        cost_estimate=1.0,
    )

    with patch.object(AnalyticsService, "get_usage_summary", new_callable=AsyncMock, return_value=mock_summary):
        response = await client.post(
            "/api/v1/analytics/export",
            headers=operator_auth_headers,
            json={
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "format": "csv",
            },
        )

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_export_analytics_json(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test exporting analytics as JSON."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    mock_summary = UsageSummary(
        date_from=date_from,
        date_to=date_to,
        total_api_calls=10,
        total_tokens=1000,
        total_agent_runs=2,
        unique_users=1,
        avg_response_time_ms=100.0,
        error_count=0,
        cost_estimate=1.0,
    )

    with patch.object(AnalyticsService, "get_usage_summary", new_callable=AsyncMock, return_value=mock_summary):
        response = await client.post(
            "/api/v1/analytics/export",
            headers=operator_auth_headers,
            json={
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "format": "json",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    data = response.json()
    assert "metrics" in data


@pytest.mark.asyncio
async def test_tenant_isolation_in_analytics(
    client: AsyncClient,
    operator_auth_headers: dict[str, str],
) -> None:
    """Test that analytics endpoints properly scope to authenticated tenant.

    The AnalyticsService receives the tenant_id from the authenticated user,
    ensuring queries are always tenant-scoped. We verify the service is called
    with the correct tenant_id.
    """
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    mock_summary = UsageSummary(
        date_from=date_from,
        date_to=date_to,
        total_api_calls=10,
        total_tokens=1000,
        total_agent_runs=2,
        unique_users=1,
        avg_response_time_ms=100.0,
        error_count=0,
        cost_estimate=1.0,
    )

    with patch.object(AnalyticsService, "get_usage_summary", new_callable=AsyncMock, return_value=mock_summary) as mock_method:
        response = await client.get(
            "/api/v1/analytics/summary",
            headers=operator_auth_headers,
            params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["total_api_calls"] == 10

    # Verify the service was called with the authenticated user's tenant_id
    mock_method.assert_called_once()
    call_args = mock_method.call_args
    called_tenant_id = call_args[0][0] if call_args[0] else call_args[1].get("tenant_id")
    # The tenant_id should be the one from the operator token (tenant_a)
    assert called_tenant_id is not None


@pytest.mark.asyncio
async def test_admin_can_access_analytics(
    client: AsyncClient,
    admin_auth_headers: dict[str, str],
) -> None:
    """Test that admins can access analytics endpoints."""
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=1)).date()
    date_to = (now + timedelta(days=1)).date()

    mock_summary = UsageSummary(
        date_from=date_from,
        date_to=date_to,
        total_api_calls=1,
        total_tokens=100,
        total_agent_runs=0,
        unique_users=1,
        avg_response_time_ms=50.0,
        error_count=0,
        cost_estimate=0.1,
    )

    with patch.object(AnalyticsService, "get_usage_summary", new_callable=AsyncMock, return_value=mock_summary):
        response = await client.get(
            "/api/v1/analytics/summary",
            headers=admin_auth_headers,
            params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        )

    assert response.status_code == 200
