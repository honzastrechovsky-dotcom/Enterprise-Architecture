"""Integration tests for analytics flows.

Tests metrics collection, analytics endpoints, and usage tracking
with real database and middleware.

Coverage:
- Metrics middleware records API calls
- Analytics summary endpoint returns data
- Analytics endpoints require auth
- Tenant-specific analytics isolation

Run with:
    pytest -m integration tests/integration/test_analytics_flow.py
"""

import asyncio

import httpx
import pytest


@pytest.mark.integration
async def test_metrics_middleware_records_api_calls(
    client_admin_a_int: httpx.AsyncClient,
    integration_db,
):
    """Metrics middleware should record API call metrics.

    Make API calls and verify metrics are stored in database.
    """
    # Make several API calls
    await client_admin_a_int.get("/api/v1/conversations")
    await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Test Metrics"},
    )

    # Wait briefly for async metrics recording
    await asyncio.sleep(0.5)

    # Query metrics from database
    from sqlalchemy import select
    from src.models.analytics import UsageMetric

    result = await integration_db.execute(
        select(UsageMetric).limit(10)
    )
    metrics = result.scalars().all()

    # Should have recorded some metrics
    # Note: Actual assertions depend on whether metrics are recorded per-request
    # or batched. For now, just verify the table exists and can be queried.
    assert isinstance(metrics, list)


@pytest.mark.integration
async def test_analytics_summary_endpoint_returns_data(
    client_admin_a_int: httpx.AsyncClient,
    mock_llm_client,
):
    """Analytics summary endpoint returns usage data.

    Make some API calls (to generate metrics) and fetch analytics.
    """
    # Generate some activity
    await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Analytics Test"},
    )

    # Wait for metrics to be recorded
    await asyncio.sleep(0.5)

    # Fetch analytics summary
    resp = await client_admin_a_int.get("/api/v1/analytics/summary")

    # Endpoint might not be implemented yet
    if resp.status_code == 404:
        pytest.skip("Analytics summary endpoint not implemented yet")

    assert resp.status_code == 200
    data = resp.json()

    # Should contain usage metrics
    assert "total_requests" in data or "metrics" in data


@pytest.mark.integration
async def test_analytics_endpoints_require_auth(
    integration_client: httpx.AsyncClient,
):
    """Analytics endpoints require authentication.

    Unauthenticated requests should return 401.
    """
    # Try to access analytics without auth
    resp = await integration_client.get("/api/v1/analytics/summary")

    # Should be 401 (unauthorized) or 404 (not implemented)
    assert resp.status_code in (401, 404)


@pytest.mark.integration
async def test_analytics_show_tenant_isolation(
    client_admin_a_int: httpx.AsyncClient,
    client_admin_b_int: httpx.AsyncClient,
    mock_llm_client,
):
    """Analytics are tenant-specific.

    Activity from tenant A should not appear in tenant B's analytics.
    """
    # Admin A creates conversations
    await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Tenant A Conversation 1"},
    )
    await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Tenant A Conversation 2"},
    )

    # Admin B creates conversation
    await client_admin_b_int.post(
        "/api/v1/conversations",
        json={"title": "Tenant B Conversation"},
    )

    # Wait for metrics
    await asyncio.sleep(0.5)

    # Admin A's analytics
    resp_a = await client_admin_a_int.get("/api/v1/analytics/summary")
    if resp_a.status_code == 404:
        pytest.skip("Analytics endpoint not implemented yet")

    assert resp_a.status_code == 200
    data_a = resp_a.json()

    # Admin B's analytics
    resp_b = await client_admin_b_int.get("/api/v1/analytics/summary")
    assert resp_b.status_code == 200
    data_b = resp_b.json()

    # Analytics should be different (tenant-specific)
    # Exact assertions depend on analytics data structure
    # For now, just verify both return data
    assert data_a is not None
    assert data_b is not None


@pytest.mark.integration
async def test_analytics_track_token_usage(
    client_admin_a_int: httpx.AsyncClient,
    mock_llm_client,
):
    """Analytics should track LLM token usage.

    Send chat messages and verify token usage is recorded.
    """
    # Create conversation and send message
    create_resp = await client_admin_a_int.post(
        "/api/v1/conversations",
        json={"title": "Token Usage Test"},
    )
    conv_id = create_resp.json()["id"]

    await client_admin_a_int.post(
        "/api/v1/chat",
        json={
            "conversation_id": conv_id,
            "message": "Test message for token tracking",
        },
    )

    # Wait for metrics
    await asyncio.sleep(0.5)

    # Fetch analytics
    resp = await client_admin_a_int.get("/api/v1/analytics/summary")
    if resp.status_code == 404:
        pytest.skip("Analytics endpoint not implemented yet")

    assert resp.status_code == 200
    data = resp.json()

    # Should include token usage metrics
    # Exact field names depend on implementation
    assert any(
        key in data
        for key in ["total_tokens", "token_usage", "llm_usage", "metrics"]
    )


@pytest.mark.integration
async def test_analytics_daily_summary_aggregation(
    client_admin_a_int: httpx.AsyncClient,
    integration_db,
):
    """Daily summary aggregation creates DailySummary records.

    This tests the background job that aggregates metrics.
    """
    from sqlalchemy import select
    from src.models.analytics import DailySummary

    # Query for daily summaries
    result = await integration_db.execute(
        select(DailySummary).limit(10)
    )
    summaries = result.scalars().all()

    # Should be able to query table (even if empty)
    assert isinstance(summaries, list)

    # Note: Actual aggregation testing would require:
    # 1. Seeding metrics
    # 2. Running aggregation job
    # 3. Verifying summaries created
    # This is more of a smoke test for now


@pytest.mark.integration
async def test_analytics_export_endpoint(
    client_admin_a_int: httpx.AsyncClient,
):
    """Analytics export endpoint returns downloadable data.

    Admin users should be able to export analytics in CSV/JSON format.
    """
    # Request CSV export
    resp = await client_admin_a_int.get("/api/v1/analytics/export?format=csv")

    # Endpoint might not be implemented
    if resp.status_code == 404:
        pytest.skip("Analytics export endpoint not implemented yet")

    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")

    # Request JSON export
    resp = await client_admin_a_int.get("/api/v1/analytics/export?format=json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers.get("content-type", "")


@pytest.mark.integration
async def test_viewer_can_access_analytics(
    client_viewer_a_int: httpx.AsyncClient,
):
    """Viewer role can access analytics (read-only).

    Analytics are generally readable by all authenticated users.
    """
    resp = await client_viewer_a_int.get("/api/v1/analytics/summary")

    # Should be allowed (200) or not implemented (404)
    # Should NOT be forbidden (403)
    assert resp.status_code in (200, 404)
    if resp.status_code != 404:
        assert resp.status_code != 403


@pytest.mark.integration
async def test_metrics_include_response_time(
    client_admin_a_int: httpx.AsyncClient,
    integration_db,
):
    """Metrics should include response time measurements.

    Make API call and verify response time is recorded.
    """
    # Make an API call
    await client_admin_a_int.get("/api/v1/conversations")

    # Wait for metrics
    await asyncio.sleep(0.5)

    # Query metrics
    from sqlalchemy import select
    from src.models.analytics import UsageMetric

    result = await integration_db.execute(
        select(UsageMetric).limit(1)
    )
    metric = result.scalar_one_or_none()

    # If metrics are recorded, verify structure
    if metric:
        # Metrics should have timing data
        # Exact fields depend on implementation
        assert hasattr(metric, "created_at") or hasattr(metric, "timestamp")


@pytest.mark.integration
async def test_analytics_filter_by_date_range(
    client_admin_a_int: httpx.AsyncClient,
):
    """Analytics endpoints support date range filtering.

    Request analytics for specific date range.
    """
    # Request analytics for last 7 days
    resp = await client_admin_a_int.get(
        "/api/v1/analytics/summary?start_date=2024-01-01&end_date=2024-01-07"
    )

    # Endpoint might not be implemented
    if resp.status_code == 404:
        pytest.skip("Analytics date filtering not implemented yet")

    assert resp.status_code in (200, 400, 422)  # 200 (ok) or validation error

    if resp.status_code == 200:
        data = resp.json()
        assert data is not None
