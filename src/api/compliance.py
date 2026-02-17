"""Compliance and governance API endpoints.

Provides compliance automation, evidence generation, and governance reporting.

GET  /api/v1/compliance/dashboard            - Compliance overview (ADMIN only)
GET  /api/v1/compliance/soc2/export          - Generate SOC 2 evidence package
POST /api/v1/compliance/gdpr/request         - Create GDPR data subject request
GET  /api/v1/compliance/gdpr/requests        - List GDPR requests
GET  /api/v1/compliance/iso27001             - ISO 27001 control mapping
POST /api/v1/compliance/test                 - Run compliance test suite
GET  /api/v1/compliance/ai-governance        - AI governance metrics
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.compliance.audit_export import SOC2ExportService
from src.compliance.dashboard import ComplianceDashboard
from src.compliance.gdpr import GDPRService, RequestType
from src.compliance.iso27001 import ISO27001Mapper
from src.compliance.testing import ComplianceTestSuite
from src.core.policy import Permission, check_permission
from src.database import get_db_session

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/compliance", tags=["compliance"])


# ------------------------------------------------------------------ #
# Response models
# ------------------------------------------------------------------ #


class ComplianceOverviewResponse(BaseModel):
    """Response model for compliance dashboard overview."""

    tenant_id: uuid.UUID
    generated_at: datetime

    # Data governance
    classification_coverage: float
    audit_completeness: float

    # Violations and incidents
    policy_violations_30d: int
    pii_incidents_30d: int
    export_control_blocks_30d: int

    # GDPR
    gdpr_requests_pending: int
    gdpr_requests_overdue: int

    # ISO 27001
    iso27001_controls_implemented: int
    iso27001_controls_total: int
    iso27001_implementation_rate: float

    # AI governance
    ai_total_interactions: int
    ai_pii_redactions: int
    ai_export_blocks: int


class AIGovernanceMetricsResponse(BaseModel):
    """Response model for AI governance metrics."""

    total_ai_interactions: int
    avg_confidence: float
    human_review_rate: float
    model_usage_by_tier: dict[str, int]
    token_budget_utilization: float
    data_lineage_coverage: float
    incident_response_time_avg: float
    hallucination_detections: int
    pii_redactions: int
    export_control_blocks: int


class GDPRRequestCreate(BaseModel):
    """Request body for creating GDPR data subject request."""

    subject_email: EmailStr = Field(..., description="Email of data subject")
    request_type: str = Field(
        ...,
        description="Type of request: 'access', 'erasure', or 'portability'",
        pattern="^(access|erasure|portability)$",
    )


class GDPRRequestResponse(BaseModel):
    """Response model for GDPR request."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    subject_email: str
    request_type: str
    status: str
    created_at: datetime
    deadline: datetime


class ISO27001ControlResponse(BaseModel):
    """Response model for ISO 27001 control."""

    annex_a_ref: str
    control_name: str
    description: str
    implemented: bool
    evidence_ref: str | None
    last_verified: datetime | None
    verification_method: str
    notes: str | None


class ComplianceTestResultResponse(BaseModel):
    """Response model for compliance test results."""

    tenant_id: uuid.UUID
    executed_at: datetime
    overall_pass: bool
    tests_passed: int
    tests_failed: int
    total_tests: int
    pass_rate: float
    test_results: list[dict[str, Any]]


# ------------------------------------------------------------------ #
# Dashboard & Overview
# ------------------------------------------------------------------ #


@router.get(
    "/dashboard",
    response_model=ComplianceOverviewResponse,
    summary="Get compliance dashboard overview",
)
async def get_compliance_dashboard(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ComplianceOverviewResponse:
    """Get compliance dashboard overview.

    Requires ADMIN role. Provides high-level compliance metrics including:
    - Classification coverage
    - Audit completeness
    - Policy violations
    - GDPR request status
    - ISO 27001 control implementation
    - AI governance metrics
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    log.info(
        "compliance.dashboard.request",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
    )

    dashboard = ComplianceDashboard(db)
    overview = await dashboard.get_overview(current_user.tenant_id)

    return ComplianceOverviewResponse(
        tenant_id=overview.tenant_id,
        generated_at=overview.generated_at,
        classification_coverage=overview.classification_coverage,
        audit_completeness=overview.audit_completeness,
        policy_violations_30d=overview.policy_violations_30d,
        pii_incidents_30d=overview.pii_incidents_30d,
        export_control_blocks_30d=overview.export_control_blocks_30d,
        gdpr_requests_pending=overview.gdpr_requests_pending,
        gdpr_requests_overdue=overview.gdpr_requests_overdue,
        iso27001_controls_implemented=overview.iso27001_controls_implemented,
        iso27001_controls_total=overview.iso27001_controls_total,
        iso27001_implementation_rate=overview.iso27001_implementation_rate,
        ai_total_interactions=overview.ai_governance_metrics.total_ai_interactions,
        ai_pii_redactions=overview.ai_governance_metrics.pii_redactions,
        ai_export_blocks=overview.ai_governance_metrics.export_control_blocks,
    )


@router.get(
    "/ai-governance",
    response_model=AIGovernanceMetricsResponse,
    summary="Get AI governance metrics",
)
async def get_ai_governance_metrics(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AIGovernanceMetricsResponse:
    """Get detailed AI governance metrics.

    Requires ADMIN role. Provides metrics on:
    - AI interaction volumes
    - Model usage by tier
    - PII redactions
    - Export control enforcement
    - Token budget utilization
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    log.info(
        "compliance.ai_governance.request",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
    )

    dashboard = ComplianceDashboard(db)
    metrics = await dashboard.get_ai_governance_metrics(current_user.tenant_id)

    return AIGovernanceMetricsResponse(
        total_ai_interactions=metrics.total_ai_interactions,
        avg_confidence=metrics.avg_confidence,
        human_review_rate=metrics.human_review_rate,
        model_usage_by_tier=metrics.model_usage_by_tier,
        token_budget_utilization=metrics.token_budget_utilization,
        data_lineage_coverage=metrics.data_lineage_coverage,
        incident_response_time_avg=metrics.incident_response_time_avg,
        hallucination_detections=metrics.hallucination_detections,
        pii_redactions=metrics.pii_redactions,
        export_control_blocks=metrics.export_control_blocks,
    )


# ------------------------------------------------------------------ #
# SOC 2 Evidence Export
# ------------------------------------------------------------------ #


@router.get(
    "/soc2/export",
    summary="Generate SOC 2 Type II evidence package",
)
async def export_soc2_evidence(
    format: str = Query("json", pattern="^(json|csv)$"),
    period_start: datetime = Query(..., description="Start of audit period (ISO 8601)"),
    period_end: datetime = Query(..., description="End of audit period (ISO 8601)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """Generate SOC 2 Type II evidence package for audit period.

    Requires ADMIN role. Generates evidence organized by Trust Service Criteria:
    - Security (CC): Access controls, authentication
    - Availability (A): Uptime, response times
    - Processing Integrity (PI): Data validation, AI processing
    - Confidentiality (C): Classification, encryption
    - Privacy (P): PII handling, GDPR compliance

    Supports JSON and CSV export formats.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    log.info(
        "compliance.soc2_export.request",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
        format=format,
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
    )

    service = SOC2ExportService(db)
    package = await service.generate_evidence_package(
        tenant_id=current_user.tenant_id,
        period_start=period_start,
        period_end=period_end,
    )

    if format == "json":
        content = service.export_to_json(package)
        media_type = "application/json"
        filename = f"soc2_evidence_{current_user.tenant_id}_{period_start.date()}_{period_end.date()}.json"
    else:  # csv
        content_bytes = service.export_to_csv(package)
        content = content_bytes.decode("utf-8")
        media_type = "text/csv"
        filename = f"soc2_evidence_{current_user.tenant_id}_{period_start.date()}_{period_end.date()}.csv"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ #
# GDPR Data Subject Rights
# ------------------------------------------------------------------ #


@router.post(
    "/gdpr/request",
    response_model=GDPRRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create GDPR data subject request",
)
async def create_gdpr_request(
    body: GDPRRequestCreate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> GDPRRequestResponse:
    """Create GDPR data subject request.

    Requires ADMIN role. Creates a request for:
    - Access (Art. 15): Export all personal data
    - Erasure (Art. 17): Delete/anonymize personal data
    - Portability (Art. 20): Export in machine-readable format

    Request must be completed within 30 days per GDPR requirements.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_WRITE)

    log.info(
        "compliance.gdpr_request.create",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
        subject_email=body.subject_email,
        request_type=body.request_type,
    )

    # Validate request type
    try:
        request_type = RequestType(body.request_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid request_type: {body.request_type}",
        )

    service = GDPRService(db)
    request = await service.create_request(
        tenant_id=current_user.tenant_id,
        subject_email=body.subject_email,
        request_type=request_type,
    )

    return GDPRRequestResponse(
        id=request.id,
        tenant_id=request.tenant_id,
        subject_email=request.subject_email,
        request_type=request.request_type,
        status=request.status,
        created_at=request.created_at,
        deadline=request.deadline,
    )


@router.get(
    "/gdpr/requests",
    response_model=list[GDPRRequestResponse],
    summary="List GDPR data subject requests",
)
async def list_gdpr_requests(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[GDPRRequestResponse]:
    """List all GDPR data subject requests for tenant.

    Requires ADMIN role. Returns all requests with their current status.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    log.info(
        "compliance.gdpr_requests.list",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
    )

    service = GDPRService(db)
    requests = await service.list_pending_requests(current_user.tenant_id)

    return [
        GDPRRequestResponse(
            id=req.id,
            tenant_id=req.tenant_id,
            subject_email=req.subject_email,
            request_type=req.request_type,
            status=req.status,
            created_at=req.created_at,
            deadline=req.deadline,
        )
        for req in requests
    ]


# ------------------------------------------------------------------ #
# ISO 27001 Control Mapping
# ------------------------------------------------------------------ #


@router.get(
    "/iso27001",
    response_model=list[ISO27001ControlResponse],
    summary="Get ISO 27001 Annex A control mapping",
)
async def get_iso27001_controls(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ISO27001ControlResponse]:
    """Get ISO 27001 Annex A control mapping.

    Requires ADMIN role. Returns mapping of platform features to ISO 27001
    controls with implementation status and evidence references.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    log.info(
        "compliance.iso27001.request",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
    )

    mapper = ISO27001Mapper(db)
    controls = await mapper.get_control_mapping(current_user.tenant_id)

    return [
        ISO27001ControlResponse(
            annex_a_ref=ctrl.annex_a_ref,
            control_name=ctrl.control_name,
            description=ctrl.description,
            implemented=ctrl.implemented,
            evidence_ref=ctrl.evidence_ref,
            last_verified=ctrl.last_verified,
            verification_method=ctrl.verification_method,
            notes=ctrl.notes,
        )
        for ctrl in controls
    ]


@router.get(
    "/iso27001/report",
    summary="Generate ISO 27001 compliance report",
)
async def generate_iso27001_report(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """Generate ISO 27001 compliance report in Markdown format.

    Requires ADMIN role. Generates comprehensive compliance report with:
    - Control implementation summary
    - Evidence references
    - Verification status
    - Gap analysis
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    log.info(
        "compliance.iso27001_report.request",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
    )

    mapper = ISO27001Mapper(db)
    report = await mapper.generate_report(current_user.tenant_id)

    filename = f"iso27001_compliance_{current_user.tenant_id}_{datetime.now().date()}.md"

    return Response(
        content=report,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ #
# Compliance Testing
# ------------------------------------------------------------------ #


@router.post(
    "/test",
    response_model=ComplianceTestResultResponse,
    summary="Run compliance test suite",
)
async def run_compliance_tests(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ComplianceTestResultResponse:
    """Run automated compliance test suite.

    Requires ADMIN role. Runs tests to verify:
    - Tenant isolation
    - Classification enforcement
    - PII redaction
    - Audit logging
    - RBAC enforcement
    - Export control
    - AI disclosure

    Returns detailed results for each test.
    """
    check_permission(current_user.role, Permission.ADMIN_TENANT_READ)

    log.info(
        "compliance.test_suite.request",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.id),
    )

    suite = ComplianceTestSuite(db)
    result = await suite.run_all(current_user.tenant_id)

    return ComplianceTestResultResponse(
        tenant_id=result.tenant_id,
        executed_at=result.executed_at,
        overall_pass=result.overall_pass,
        tests_passed=result.tests_passed,
        tests_failed=result.tests_failed,
        total_tests=result.total_tests,
        pass_rate=result.pass_rate,
        test_results=[
            {
                "test_name": test.test_name,
                "passed": test.passed,
                "message": test.message,
                "evidence": test.evidence,
                "severity": test.severity,
            }
            for test in result.test_results
        ],
    )
