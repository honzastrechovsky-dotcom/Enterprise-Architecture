"""Compliance Administration API - automated compliance checks and evidence.

Admin-only endpoints for running compliance checks, retrieving historical
reports, generating evidence packages for audit standards, and managing
the compliance check schedule.

POST /api/v1/compliance/check              - Run compliance check now
GET  /api/v1/compliance/reports            - List compliance reports
GET  /api/v1/compliance/reports/{id}       - Get specific report
GET  /api/v1/compliance/status             - Current compliance status
POST /api/v1/compliance/evidence/{standard} - Generate evidence package
GET  /api/v1/compliance/schedule           - Get check schedule configuration
PUT  /api/v1/compliance/schedule           - Update check schedule

All endpoints require the 'admin' role.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.compliance.evidence import (
    EvidencePackage,
    collect_gdpr_evidence,
    collect_iso27001_evidence,
    collect_soc2_evidence,
)
from src.compliance.scheduler import ComplianceRun, ComplianceScheduler, RunTrigger, ScheduleConfig
from src.core.policy import Permission, check_permission
from src.database import get_db_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/compliance", tags=["compliance-admin"])

# ---------------------------------------------------------------------------
# Supported evidence standards
# ---------------------------------------------------------------------------

_SUPPORTED_STANDARDS = {"soc2", "gdpr", "iso27001"}


# ---------------------------------------------------------------------------
# Pydantic response / request models
# ---------------------------------------------------------------------------


class FindingResponse(BaseModel):
    """Single finding within a compliance report."""

    check_name: str
    status: str
    details: str
    evidence: dict[str, Any]
    remediation_suggestion: str | None


class ComplianceRunResponse(BaseModel):
    """Summary of a compliance run."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    triggered_by: str
    started_at: datetime
    completed_at: datetime | None
    overall_status: str
    score: float
    findings: list[FindingResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ComplianceRunSummary(BaseModel):
    """Lightweight summary for list views."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    triggered_by: str
    started_at: datetime
    completed_at: datetime | None
    overall_status: str
    score: float

    model_config = {"from_attributes": True}


class ComplianceRunListResponse(BaseModel):
    """Paginated list of compliance runs."""

    items: list[ComplianceRunSummary]
    total: int
    limit: int
    offset: int


class ComplianceStatusResponse(BaseModel):
    """Current compliance status snapshot."""

    tenant_id: uuid.UUID
    as_of: datetime
    overall_status: str
    score: float
    last_run_at: datetime | None
    last_run_id: uuid.UUID | None


class CheckNowRequest(BaseModel):
    """Body for POST /compliance/check."""

    comment: str | None = Field(None, description="Optional comment for audit trail")


class CheckNowResponse(BaseModel):
    """Response from triggering an immediate compliance check."""

    run_id: uuid.UUID
    tenant_id: uuid.UUID
    triggered_by: str
    started_at: datetime
    overall_status: str
    score: float
    num_findings: int
    message: str


class EvidencePackageResponse(BaseModel):
    """Response from evidence package generation."""

    standard: str
    generated_at: datetime
    tenant_id: uuid.UUID
    num_controls: int
    evidence_files: list[str]
    package: dict[str, Any]


class ScheduleConfigRequest(BaseModel):
    """Body for PUT /compliance/schedule."""

    daily_enabled: bool = True
    weekly_enabled: bool = True
    monthly_enabled: bool = True
    daily_hour_utc: int = Field(2, ge=0, le=23)
    weekly_day: int = Field(1, ge=1, le=7, description="ISO weekday: 1=Monday, 7=Sunday")
    monthly_day: int = Field(1, ge=1, le=28, description="Day of month (1-28)")


class ScheduleConfigResponse(BaseModel):
    """Current compliance check schedule."""

    daily_enabled: bool
    weekly_enabled: bool
    monthly_enabled: bool
    daily_hour_utc: int
    weekly_day: int
    monthly_day: int
    next_scheduled_check: str | None = Field(
        None,
        description="ISO 8601 timestamp of next expected scheduled run",
    )


# ---------------------------------------------------------------------------
# In-memory schedule store (production would use a settings table)
# ---------------------------------------------------------------------------

# Default schedule configuration – shared across all tenants for now.
# In production this would be stored in a per-tenant settings table.
_schedule_config = ScheduleConfig()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_to_response(run: ComplianceRun) -> ComplianceRunResponse:
    """Convert an ORM ComplianceRun to the API response model."""
    findings_resp = [
        FindingResponse(
            check_name=f.check_name,
            status=f.status,
            details=f.details,
            evidence=f.evidence,
            remediation_suggestion=f.remediation_suggestion,
        )
        for f in (run.findings or [])
    ]
    return ComplianceRunResponse(
        id=run.id,
        tenant_id=run.tenant_id,
        triggered_by=run.triggered_by,
        started_at=run.started_at,
        completed_at=run.completed_at,
        overall_status=run.overall_status,
        score=run.score,
        findings=findings_resp,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/check",
    response_model=CheckNowResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run compliance check now (admin only)",
)
async def run_compliance_check(
    body: CheckNowRequest | None = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> CheckNowResponse:
    """Trigger an immediate compliance check for the current tenant.

    Executes all six compliance checks synchronously, persists results,
    and returns the completed run summary.

    Requires: admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    tenant_id = current_user.tenant_id

    if body and body.comment:
        log.info(
            "compliance.admin.check_triggered",
            tenant_id=str(tenant_id),
            triggered_by_user=str(current_user.user_id),
            comment=body.comment,
        )

    scheduler = ComplianceScheduler(db)
    run = await scheduler.run_now(tenant_id, triggered_by=RunTrigger.MANUAL)

    await db.commit()

    return CheckNowResponse(
        run_id=run.id,
        tenant_id=run.tenant_id,
        triggered_by=run.triggered_by,
        started_at=run.started_at,
        overall_status=run.overall_status,
        score=run.score,
        num_findings=len(run.findings or []),
        message=(
            f"Compliance check completed. Score: {run.score}/100. "
            f"Status: {run.overall_status}."
        ),
    )


@router.get(
    "/reports",
    response_model=ComplianceRunListResponse,
    summary="List compliance reports (admin only)",
)
async def list_compliance_reports(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ComplianceRunListResponse:
    """List compliance check runs for the current tenant, newest first.

    Returns:
        Paginated list of compliance run summaries.

    Requires: admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    scheduler = ComplianceScheduler(db)
    runs = await scheduler.list_runs(
        tenant_id=current_user.tenant_id,
        limit=limit,
        offset=offset,
    )

    summaries = [
        ComplianceRunSummary(
            id=r.id,
            tenant_id=r.tenant_id,
            triggered_by=r.triggered_by,
            started_at=r.started_at,
            completed_at=r.completed_at,
            overall_status=r.overall_status,
            score=r.score,
        )
        for r in runs
    ]

    return ComplianceRunListResponse(
        items=summaries,
        total=len(summaries),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/reports/{report_id}",
    response_model=ComplianceRunResponse,
    summary="Get specific compliance report (admin only)",
)
async def get_compliance_report(
    report_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ComplianceRunResponse:
    """Fetch a specific compliance run with all findings.

    Args:
        report_id: UUID of the compliance run.

    Returns:
        Full ComplianceRun with findings and remediation suggestions.

    Raises:
        404 if the run does not exist or belongs to another tenant.

    Requires: admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    scheduler = ComplianceScheduler(db)
    run = await scheduler.get_run(report_id, current_user.tenant_id)

    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Compliance report {report_id} not found.",
        )

    return _run_to_response(run)


@router.get(
    "/status",
    response_model=ComplianceStatusResponse,
    summary="Get current compliance status (admin only)",
)
async def get_compliance_status(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ComplianceStatusResponse:
    """Return the current compliance status based on the most recent run.

    If no runs exist, returns a SKIP status with score 0.

    Requires: admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    scheduler = ComplianceScheduler(db)
    latest = await scheduler.get_latest_run(current_user.tenant_id)

    if latest is None:
        return ComplianceStatusResponse(
            tenant_id=current_user.tenant_id,
            as_of=datetime.now(datetime.now().astimezone().tzinfo),
            overall_status="SKIP",
            score=0.0,
            last_run_at=None,
            last_run_id=None,
        )


    return ComplianceStatusResponse(
        tenant_id=current_user.tenant_id,
        as_of=datetime.now(UTC),
        overall_status=latest.overall_status,
        score=latest.score,
        last_run_at=latest.started_at,
        last_run_id=latest.id,
    )


@router.post(
    "/evidence/{standard}",
    response_model=EvidencePackageResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate evidence package for compliance standard (admin only)",
)
async def generate_evidence_package(
    standard: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> EvidencePackageResponse:
    """Generate an automated evidence package for the specified standard.

    Supported standards:
    - **soc2**: SOC 2 Type II Trust Service Criteria
    - **gdpr**: EU General Data Protection Regulation
    - **iso27001**: ISO 27001:2022 Annex A Controls

    The returned package contains structured JSON evidence files suitable
    for attachment to audit workpapers.

    Raises:
        400 if the standard is not supported.

    Requires: admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    standard_lower = standard.lower()
    if standard_lower not in _SUPPORTED_STANDARDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported standard {standard!r}. "
                f"Supported: {sorted(_SUPPORTED_STANDARDS)}"
            ),
        )

    tenant_id = current_user.tenant_id

    log.info(
        "compliance.admin.evidence_requested",
        tenant_id=str(tenant_id),
        standard=standard_lower,
        user_id=str(current_user.user_id),
    )

    if standard_lower == "soc2":
        package: EvidencePackage = await collect_soc2_evidence(db, tenant_id)
    elif standard_lower == "gdpr":
        package = await collect_gdpr_evidence(db, tenant_id)
    else:  # iso27001
        package = await collect_iso27001_evidence(db, tenant_id)

    return EvidencePackageResponse(
        standard=package.standard,
        generated_at=package.generated_at,
        tenant_id=tenant_id,
        num_controls=len(package.controls),
        evidence_files=list(package.evidence_files.keys()),
        package=package.to_dict(),
    )


@router.get(
    "/schedule",
    response_model=ScheduleConfigResponse,
    summary="Get compliance check schedule (admin only)",
)
async def get_compliance_schedule(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> ScheduleConfigResponse:
    """Return the current compliance check schedule configuration.

    Requires: admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    return ScheduleConfigResponse(
        daily_enabled=_schedule_config.daily_enabled,
        weekly_enabled=_schedule_config.weekly_enabled,
        monthly_enabled=_schedule_config.monthly_enabled,
        daily_hour_utc=_schedule_config.daily_hour_utc,
        weekly_day=_schedule_config.weekly_day,
        monthly_day=_schedule_config.monthly_day,
        next_scheduled_check=_next_check_description(_schedule_config),
    )


@router.put(
    "/schedule",
    response_model=ScheduleConfigResponse,
    summary="Update compliance check schedule (admin only)",
)
async def update_compliance_schedule(
    body: ScheduleConfigRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> ScheduleConfigResponse:
    """Update the compliance check schedule configuration.

    Changes take effect on the next scheduler tick.

    Requires: admin role.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    global _schedule_config
    _schedule_config = ScheduleConfig(
        daily_enabled=body.daily_enabled,
        weekly_enabled=body.weekly_enabled,
        monthly_enabled=body.monthly_enabled,
        daily_hour_utc=body.daily_hour_utc,
        weekly_day=body.weekly_day,
        monthly_day=body.monthly_day,
    )

    log.info(
        "compliance.admin.schedule_updated",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.user_id),
        daily_enabled=_schedule_config.daily_enabled,
        weekly_enabled=_schedule_config.weekly_enabled,
        monthly_enabled=_schedule_config.monthly_enabled,
    )

    return ScheduleConfigResponse(
        daily_enabled=_schedule_config.daily_enabled,
        weekly_enabled=_schedule_config.weekly_enabled,
        monthly_enabled=_schedule_config.monthly_enabled,
        daily_hour_utc=_schedule_config.daily_hour_utc,
        weekly_day=_schedule_config.weekly_day,
        monthly_day=_schedule_config.monthly_day,
        next_scheduled_check=_next_check_description(_schedule_config),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _next_check_description(config: ScheduleConfig) -> str | None:
    """Human-readable description of the next scheduled check."""
    if not any([config.daily_enabled, config.weekly_enabled, config.monthly_enabled]):
        return None


    now = datetime.now(UTC)

    if config.daily_enabled:
        next_daily = now.replace(
            hour=config.daily_hour_utc,
            minute=0,
            second=0,
            microsecond=0,
        )
        from datetime import timedelta

        if next_daily <= now:
            next_daily = next_daily + timedelta(days=1)
        return next_daily.isoformat()

    return None
