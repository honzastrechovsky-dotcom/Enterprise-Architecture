"""Analytics API endpoints.

GET  /api/v1/analytics/summary  - Usage summary
GET  /api/v1/analytics/tokens   - Token usage by model
GET  /api/v1/analytics/agents   - Agent performance
GET  /api/v1/analytics/users    - Top users
GET  /api/v1/analytics/costs    - Cost breakdown
GET  /api/v1/analytics/errors   - Error rates
GET  /api/v1/analytics/trends   - Daily trends
POST /api/v1/analytics/export   - Export analytics data

All endpoints are operator/admin only and tenant-scoped.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import structlog
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.api_key_auth import require_scope
from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.policy import Permission, check_permission
from src.database import get_db_session
from src.services.analytics import (
    AgentPerformance,
    AnalyticsService,
    CostBreakdown,
    DailyTrend,
    ErrorRate,
    ModelUsage,
    UsageSummary,
    UserUsage,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"], dependencies=[Depends(require_scope("analytics"))])


# Request models
class ExportRequest(BaseModel):
    """Request body for exporting analytics data."""

    date_from: date
    date_to: date
    format: str = "csv"  # "csv" or "json"


# Response models
class TokenUsageResponse(BaseModel):
    """Token usage response."""

    usage: list[ModelUsage]


class AgentPerformanceResponse(BaseModel):
    """Agent performance response."""

    performance: list[AgentPerformance]


class TopUsersResponse(BaseModel):
    """Top users response."""

    users: list[UserUsage]


class DailyTrendsResponse(BaseModel):
    """Daily trends response."""

    trends: list[DailyTrend]


@router.get(
    "/summary",
    response_model=UsageSummary,
    summary="Get usage summary for date range",
)
async def get_analytics_summary(
    date_from: date = Query(..., description="Start date (inclusive)"),
    date_to: date = Query(..., description="End date (inclusive)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UsageSummary:
    """Get overall usage summary for the authenticated tenant."""
    check_permission(current_user.role, Permission.ANALYTICS_READ)

    service = AnalyticsService(db)
    return await service.get_usage_summary(current_user.tenant_id, date_from, date_to)


@router.get(
    "/tokens",
    response_model=TokenUsageResponse,
    summary="Get token usage by model",
)
async def get_token_usage_by_model(
    date_from: date = Query(..., description="Start date (inclusive)"),
    date_to: date = Query(..., description="End date (inclusive)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TokenUsageResponse:
    """Get token usage broken down by model."""
    check_permission(current_user.role, Permission.ANALYTICS_READ)

    service = AnalyticsService(db)
    usage = await service.get_token_usage_by_model(current_user.tenant_id, date_from, date_to)
    return TokenUsageResponse(usage=usage)


@router.get(
    "/agents",
    response_model=AgentPerformanceResponse,
    summary="Get agent performance metrics",
)
async def get_agent_performance(
    date_from: date = Query(..., description="Start date (inclusive)"),
    date_to: date = Query(..., description="End date (inclusive)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentPerformanceResponse:
    """Get agent performance metrics."""
    check_permission(current_user.role, Permission.ANALYTICS_READ)

    service = AnalyticsService(db)
    performance = await service.get_agent_performance(current_user.tenant_id, date_from, date_to)
    return AgentPerformanceResponse(performance=performance)


@router.get(
    "/users",
    response_model=TopUsersResponse,
    summary="Get top users by activity",
)
async def get_top_users(
    date_from: date = Query(..., description="Start date (inclusive)"),
    date_to: date = Query(..., description="End date (inclusive)"),
    limit: int = Query(10, ge=1, le=100, description="Number of users to return"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TopUsersResponse:
    """Get top users by activity."""
    check_permission(current_user.role, Permission.ANALYTICS_READ)

    service = AnalyticsService(db)
    users = await service.get_top_users(current_user.tenant_id, date_from, date_to, limit)
    return TopUsersResponse(users=users)


@router.get(
    "/costs",
    response_model=CostBreakdown,
    summary="Get cost breakdown",
)
async def get_cost_breakdown(
    date_from: date = Query(..., description="Start date (inclusive)"),
    date_to: date = Query(..., description="End date (inclusive)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> CostBreakdown:
    """Get cost breakdown by model."""
    check_permission(current_user.role, Permission.ANALYTICS_READ)

    service = AnalyticsService(db)
    return await service.get_cost_breakdown(current_user.tenant_id, date_from, date_to)


@router.get(
    "/errors",
    response_model=ErrorRate,
    summary="Get error rate",
)
async def get_error_rate(
    date_from: date = Query(..., description="Start date (inclusive)"),
    date_to: date = Query(..., description="End date (inclusive)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ErrorRate:
    """Get error rate for API calls."""
    check_permission(current_user.role, Permission.ANALYTICS_READ)

    service = AnalyticsService(db)
    return await service.get_error_rate(current_user.tenant_id, date_from, date_to)


@router.get(
    "/trends",
    response_model=DailyTrendsResponse,
    summary="Get daily trends",
)
async def get_daily_trends(
    days: int = Query(30, ge=1, le=365, description="Number of days to retrieve"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> DailyTrendsResponse:
    """Get daily trend data from pre-aggregated summaries."""
    check_permission(current_user.role, Permission.ANALYTICS_READ)

    service = AnalyticsService(db)
    trends = await service.get_daily_trends(current_user.tenant_id, days)
    return DailyTrendsResponse(trends=trends)


@router.post(
    "/export",
    status_code=status.HTTP_200_OK,
    summary="Export analytics data",
)
async def export_analytics(
    export_request: ExportRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """Export analytics data as CSV or JSON."""
    check_permission(current_user.role, Permission.ANALYTICS_READ)

    service = AnalyticsService(db)
    summary = await service.get_usage_summary(
        current_user.tenant_id,
        export_request.date_from,
        export_request.date_to,
    )

    if export_request.format == "csv":
        # Generate CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow([
            "Metric",
            "Value",
        ])

        # Data
        writer.writerow(["Date From", summary.date_from.isoformat()])
        writer.writerow(["Date To", summary.date_to.isoformat()])
        writer.writerow(["Total API Calls", summary.total_api_calls])
        writer.writerow(["Total Tokens", summary.total_tokens])
        writer.writerow(["Total Agent Runs", summary.total_agent_runs])
        writer.writerow(["Unique Users", summary.unique_users])
        writer.writerow(["Avg Response Time (ms)", f"{summary.avg_response_time_ms:.2f}"])
        writer.writerow(["Error Count", summary.error_count])
        writer.writerow(["Cost Estimate ($)", f"{summary.cost_estimate:.2f}"])

        csv_content = output.getvalue()
        output.close()

        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=analytics_{export_request.date_from}_{export_request.date_to}.csv"
            },
        )
    else:
        # JSON format
        data = {
            "date_from": summary.date_from.isoformat(),
            "date_to": summary.date_to.isoformat(),
            "metrics": {
                "total_api_calls": summary.total_api_calls,
                "total_tokens": summary.total_tokens,
                "total_agent_runs": summary.total_agent_runs,
                "unique_users": summary.unique_users,
                "avg_response_time_ms": summary.avg_response_time_ms,
                "error_count": summary.error_count,
                "cost_estimate": summary.cost_estimate,
            },
        }

        import json
        json_content = json.dumps(data, indent=2)

        return Response(
            content=json_content,
            media_type="application/json",
            headers={
                "Content-Disposition": f"attachment; filename=analytics_{export_request.date_from}_{export_request.date_to}.json"
            },
        )
