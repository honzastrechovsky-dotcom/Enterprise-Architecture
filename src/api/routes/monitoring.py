"""API routes for proactive MES monitoring.

Endpoints:
- GET  /monitoring/thresholds          - List current thresholds for tenant
- PUT  /monitoring/thresholds          - Update thresholds
- GET  /monitoring/alerts              - List recent alerts (paginated)
- POST /monitoring/alerts/{id}/acknowledge - Acknowledge an alert

All endpoints enforce tenant isolation via the authenticated user's tenant_id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.database import get_db_session
from src.models.monitoring import (
    AlertSeverity,
    AlertStatus,
    AlertThreshold,
    MonitoringAlert,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/monitoring", tags=["monitoring"])


# -----------------------------------------------------------------------
# Request / Response schemas
# -----------------------------------------------------------------------


class ThresholdResponse(BaseModel):
    """Single alert threshold configuration."""

    id: str
    metric_name: str
    threshold_value: float
    severity: str
    enabled: bool
    created_at: str
    updated_at: str


class ThresholdListResponse(BaseModel):
    """List of alert thresholds for a tenant."""

    thresholds: list[ThresholdResponse]
    count: int


class ThresholdUpdateItem(BaseModel):
    """Single threshold update."""

    metric_name: str = Field(
        ...,
        description="Metric name, e.g. 'machine_downtime_minutes'",
        examples=["machine_downtime_minutes"],
    )
    threshold_value: float = Field(
        ...,
        description="Numeric threshold value",
        ge=0,
        examples=[30.0],
    )
    severity: str = Field(
        default="warning",
        description="Alert severity: info, warning, critical",
        examples=["warning"],
    )
    enabled: bool = Field(
        default=True,
        description="Whether this threshold is active",
    )


class ThresholdUpdateRequest(BaseModel):
    """Batch threshold update request."""

    thresholds: list[ThresholdUpdateItem] = Field(
        ...,
        description="List of thresholds to create or update",
        min_length=1,
    )


class AlertResponse(BaseModel):
    """Single monitoring alert."""

    id: str
    metric_name: str
    current_value: float
    threshold_value: float
    severity: str
    status: str
    message: str | None
    recommended_action: str | None
    created_at: str
    acknowledged_at: str | None
    acknowledged_by: str | None
    resolved_at: str | None


class AlertListResponse(BaseModel):
    """Paginated list of monitoring alerts."""

    alerts: list[AlertResponse]
    total: int
    page: int
    page_size: int


class AcknowledgeRequest(BaseModel):
    """Alert acknowledgement request."""

    note: str | None = Field(
        default=None,
        description="Optional note from the acknowledging user",
        max_length=1000,
    )


class AcknowledgeResponse(BaseModel):
    """Alert acknowledgement result."""

    id: str
    status: str
    acknowledged_at: str
    acknowledged_by: str


# -----------------------------------------------------------------------
# Helper: map ORM -> response
# -----------------------------------------------------------------------


def _threshold_to_response(t: AlertThreshold) -> ThresholdResponse:
    return ThresholdResponse(
        id=str(t.id),
        metric_name=t.metric_name,
        threshold_value=t.threshold_value,
        severity=t.severity,
        enabled=t.enabled,
        created_at=t.created_at.isoformat(),
        updated_at=t.updated_at.isoformat(),
    )


def _alert_to_response(a: MonitoringAlert) -> AlertResponse:
    return AlertResponse(
        id=str(a.id),
        metric_name=a.metric_name,
        current_value=a.current_value,
        threshold_value=a.threshold_value,
        severity=a.severity,
        status=a.status,
        message=a.message,
        recommended_action=a.recommended_action,
        created_at=a.created_at.isoformat(),
        acknowledged_at=a.acknowledged_at.isoformat() if a.acknowledged_at else None,
        acknowledged_by=a.acknowledged_by,
        resolved_at=a.resolved_at.isoformat() if a.resolved_at else None,
    )


# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------


@router.get("/thresholds", response_model=ThresholdListResponse)
async def list_thresholds(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ThresholdListResponse:
    """List all alert thresholds configured for the current tenant."""
    stmt = (
        select(AlertThreshold)
        .where(AlertThreshold.tenant_id == current_user.tenant_id)
        .order_by(AlertThreshold.metric_name)
    )
    result = await db.execute(stmt)
    thresholds = result.scalars().all()

    log.info(
        "monitoring.thresholds_listed",
        tenant_id=str(current_user.tenant_id),
        count=len(thresholds),
    )

    return ThresholdListResponse(
        thresholds=[_threshold_to_response(t) for t in thresholds],
        count=len(thresholds),
    )


@router.put("/thresholds", response_model=ThresholdListResponse)
async def update_thresholds(
    request: ThresholdUpdateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ThresholdListResponse:
    """Create or update alert thresholds for the current tenant.

    Performs an upsert: if a threshold for the given metric_name already
    exists for this tenant, it is updated. Otherwise, a new row is created.
    """
    tenant_id = current_user.tenant_id
    updated: list[AlertThreshold] = []

    for item in request.thresholds:
        # Validate severity
        try:
            severity = AlertSeverity(item.severity)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid severity '{item.severity}'. Must be one of: info, warning, critical",
            )

        # Check if threshold exists
        stmt = select(AlertThreshold).where(
            and_(
                AlertThreshold.tenant_id == tenant_id,
                AlertThreshold.metric_name == item.metric_name,
            )
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            existing.threshold_value = item.threshold_value
            existing.severity = severity
            existing.enabled = item.enabled
            existing.updated_at = datetime.now(UTC)
            updated.append(existing)
        else:
            threshold = AlertThreshold(
                tenant_id=tenant_id,
                metric_name=item.metric_name,
                threshold_value=item.threshold_value,
                severity=severity,
                enabled=item.enabled,
            )
            db.add(threshold)
            updated.append(threshold)

    await db.flush()

    log.info(
        "monitoring.thresholds_updated",
        tenant_id=str(tenant_id),
        user_id=str(current_user.id),
        count=len(updated),
        metrics=[t.metric_name for t in updated],
    )

    return ThresholdListResponse(
        thresholds=[_threshold_to_response(t) for t in updated],
        count=len(updated),
    )


@router.get("/alerts", response_model=AlertListResponse)
async def list_alerts(
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Filter by status: open, acknowledged, resolved",
    ),
    severity_filter: str | None = Query(
        default=None,
        alias="severity",
        description="Filter by severity: info, warning, critical",
    ),
    metric_filter: str | None = Query(
        default=None,
        alias="metric",
        description="Filter by metric name",
    ),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AlertListResponse:
    """List recent monitoring alerts for the current tenant.

    Supports pagination and filtering by status, severity, and metric name.
    Results are ordered by created_at descending (most recent first).
    """
    tenant_id = current_user.tenant_id

    # Build WHERE clause
    conditions = [MonitoringAlert.tenant_id == tenant_id]

    if status_filter:
        try:
            AlertStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{status_filter}'. Must be one of: open, acknowledged, resolved",
            )
        conditions.append(MonitoringAlert.status == status_filter)

    if severity_filter:
        try:
            AlertSeverity(severity_filter)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid severity '{severity_filter}'. Must be one of: info, warning, critical",
            )
        conditions.append(MonitoringAlert.severity == severity_filter)

    if metric_filter:
        conditions.append(MonitoringAlert.metric_name == metric_filter)

    where_clause = and_(*conditions)

    # Count total
    count_stmt = select(func.count()).select_from(MonitoringAlert).where(where_clause)
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    # Fetch page
    offset = (page - 1) * page_size
    stmt = (
        select(MonitoringAlert)
        .where(where_clause)
        .order_by(MonitoringAlert.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    alerts = result.scalars().all()

    log.info(
        "monitoring.alerts_listed",
        tenant_id=str(tenant_id),
        total=total,
        page=page,
        page_size=page_size,
        returned=len(alerts),
    )

    return AlertListResponse(
        alerts=[_alert_to_response(a) for a in alerts],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/alerts/{alert_id}/acknowledge",
    response_model=AcknowledgeResponse,
)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    request: AcknowledgeRequest | None = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AcknowledgeResponse:
    """Acknowledge an open monitoring alert.

    Only open alerts can be acknowledged. Attempting to acknowledge an
    already-acknowledged or resolved alert returns 409 Conflict.
    """
    tenant_id = current_user.tenant_id

    stmt = select(MonitoringAlert).where(
        and_(
            MonitoringAlert.id == alert_id,
            MonitoringAlert.tenant_id == tenant_id,
        )
    )
    result = await db.execute(stmt)
    alert = result.scalar_one_or_none()

    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Alert {alert_id} not found",
        )

    if alert.status != AlertStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Alert is already {alert.status}. Only open alerts can be acknowledged.",
        )

    now = datetime.now(UTC)
    alert.status = AlertStatus.ACKNOWLEDGED
    alert.acknowledged_at = now
    alert.acknowledged_by = current_user.email
    await db.flush()

    log.info(
        "monitoring.alert_acknowledged",
        alert_id=str(alert_id),
        tenant_id=str(tenant_id),
        user=current_user.email,
        metric=alert.metric_name,
    )

    return AcknowledgeResponse(
        id=str(alert.id),
        status=alert.status,
        acknowledged_at=now.isoformat(),
        acknowledged_by=current_user.email,
    )
