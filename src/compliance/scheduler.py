"""Compliance Scheduler - periodic automated compliance checks.

Manages scheduled and on-demand compliance runs, persists results to
database, and detects compliance drift (status changes from PASS to FAIL).

Architecture:
- ComplianceRun: persisted record of each run (scheduled or manual)
- ComplianceFinding: each individual check result within a run
- ComplianceScheduler: orchestrates runs, stores results, compares with prior

Drift detection:
- After each run, the scheduler compares the current overall_status with
  the most recent previous run for the same tenant.
- If the status degrades (e.g. PASS → FAIL or PASS → WARNING) an alert is
  logged with structured context for downstream alerting (PagerDuty, Slack, etc.)

Design decisions:
- Alembic migration 011_compliance_runs.py creates the tables
- All timestamps are timezone-aware UTC
- JSONB report column stores the full ComplianceReport.to_dict() output
  for historical fidelity without requiring re-computation
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.compliance.monitor import CheckStatus, ComplianceMonitor, ComplianceReport
from src.database import Base

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class RunTrigger(StrEnum):
    """How a compliance run was initiated."""

    SCHEDULED = "scheduled"
    MANUAL = "manual"


class ComplianceRun(Base):
    """Persisted record of a compliance check run."""

    __tablename__ = "compliance_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    triggered_by: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RunTrigger.MANUAL,
        comment="scheduled | manual",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    overall_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CheckStatus.SKIP,
        comment="PASS | FAIL | WARNING | SKIP",
    )
    score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        comment="Compliance score 0-100",
    )
    report: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Full ComplianceReport serialised as JSON",
    )

    # Relationships
    findings: Mapped[list[ComplianceFinding]] = relationship(
        "ComplianceFinding",
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_compliance_runs_tenant_started", "tenant_id", "started_at"),
        Index("ix_compliance_runs_tenant_status", "tenant_id", "overall_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ComplianceRun id={self.id} tenant={self.tenant_id} "
            f"status={self.overall_status} score={self.score}>"
        )


class ComplianceFinding(Base):
    """Individual check result within a compliance run."""

    __tablename__ = "compliance_findings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("compliance_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    check_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="PASS | FAIL | WARNING | SKIP",
    )
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    remediation_suggestion: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Actionable remediation steps for non-PASS findings",
    )

    # Relationship back to the run
    run: Mapped[ComplianceRun] = relationship(
        "ComplianceRun",
        back_populates="findings",
    )

    __table_args__ = (
        Index("ix_compliance_findings_run_check", "run_id", "check_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<ComplianceFinding run={self.run_id} check={self.check_name!r} "
            f"status={self.status}>"
        )


# ---------------------------------------------------------------------------
# Remediation suggestions (static lookup)
# ---------------------------------------------------------------------------

_REMEDIATION: dict[str, dict[str, str]] = {
    "data_classification": {
        CheckStatus.FAIL: (
            "Run the classification sweep: POST /api/v1/documents/classify-all. "
            "Enforce classification at ingestion by configuring the document policy."
        ),
        CheckStatus.WARNING: (
            "Locate unclassified documents via GET /api/v1/documents?classification=null "
            "and apply appropriate labels."
        ),
    },
    "access_controls": {
        CheckStatus.FAIL: (
            "Ensure at least one admin user exists (POST /api/v1/admin/users). "
            "If auth failure rate is elevated, review login attempts and consider "
            "enabling rate limiting / account lockout."
        ),
        CheckStatus.WARNING: (
            "Review admin role assignments. Apply the principle of least privilege by "
            "demoting users who do not need admin access."
        ),
    },
    "encryption": {
        CheckStatus.FAIL: (
            "Verify TLS certificates are valid and HTTPS is enforced on all endpoints. "
            "Confirm KMS integration is active for database at-rest encryption."
        ),
        CheckStatus.WARNING: (
            "Review encryption configuration. Ensure all S3 buckets have SSE-KMS enabled."
        ),
    },
    "audit_logging": {
        CheckStatus.FAIL: (
            "Check application logs for errors in the audit middleware. "
            "Verify AuditService is wired into all API routes."
        ),
        CheckStatus.WARNING: (
            "Investigate why no audit events have been created in the last 24 hours. "
            "Check if the service has been idle or if logging middleware was bypassed."
        ),
    },
    "data_retention": {
        CheckStatus.FAIL: (
            "Run the retention purge job immediately: "
            "POST /api/v1/admin/maintenance/purge-expired-data. "
            "Review the scheduled purge configuration."
        ),
        CheckStatus.WARNING: (
            "Schedule a retention purge run soon to bring the tenant within policy."
        ),
    },
    "pii_handling": {
        CheckStatus.FAIL: (
            "Enable PII detection in the document pipeline. "
            "Configure the PII policy: PUT /api/v1/admin/pii-policy."
        ),
        CheckStatus.WARNING: (
            "Verify that the PII detection service is processing new documents. "
            "Check pipeline logs for silent failures."
        ),
    },
}


def _remediation_suggestion(check_name: str, status: CheckStatus) -> str | None:
    """Return a remediation suggestion for a non-PASS finding, or None."""
    if status == CheckStatus.PASS or status == CheckStatus.SKIP:
        return None
    return _REMEDIATION.get(check_name, {}).get(status)


# ---------------------------------------------------------------------------
# ComplianceScheduler
# ---------------------------------------------------------------------------


@dataclass
class ScheduleConfig:
    """Configuration for periodic compliance runs."""

    daily_enabled: bool = True
    weekly_enabled: bool = True
    monthly_enabled: bool = True
    daily_hour_utc: int = 2       # 02:00 UTC
    weekly_day: int = 1           # Monday (ISO weekday)
    monthly_day: int = 1          # 1st of the month


class ComplianceScheduler:
    """Orchestrate periodic compliance checks and persist results.

    Usage::

        scheduler = ComplianceScheduler(db)

        # Run checks now (manual trigger)
        run = await scheduler.run_now(tenant_id)

        # After a scheduled run
        run = await scheduler.run_now(tenant_id, triggered_by="scheduled")
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._monitor = ComplianceMonitor(db)

    async def run_now(
        self,
        tenant_id: uuid.UUID,
        triggered_by: str = RunTrigger.MANUAL,
    ) -> ComplianceRun:
        """Execute a full compliance check and persist the results.

        Steps:
        1. Create ComplianceRun record (status=SKIP until complete)
        2. Run all checks via ComplianceMonitor
        3. Persist ComplianceFinding records for each check
        4. Update ComplianceRun with final status / score
        5. Detect drift vs previous run and emit alert if degraded

        Args:
            tenant_id: Tenant to check.
            triggered_by: "scheduled" or "manual".

        Returns:
            Completed ComplianceRun ORM object (includes findings via relationship).
        """
        log.info(
            "compliance.scheduler.run_started",
            tenant_id=str(tenant_id),
            triggered_by=triggered_by,
        )

        started_at = datetime.now(UTC)

        # Step 1: Create run record
        run = ComplianceRun(
            tenant_id=tenant_id,
            triggered_by=triggered_by,
            started_at=started_at,
            overall_status=CheckStatus.SKIP,
            score=0.0,
            report={},
        )
        self._db.add(run)
        await self._db.flush()  # Assign ID before creating findings

        # Step 2: Execute compliance checks
        report: ComplianceReport = await self._monitor.run_all_checks(tenant_id)

        # Step 3: Persist findings
        for check_result in report.checks:
            suggestion = _remediation_suggestion(check_result.check_name, check_result.status)
            finding = ComplianceFinding(
                run_id=run.id,
                check_name=check_result.check_name,
                status=check_result.status,
                details=check_result.details,
                evidence=check_result.evidence,
                remediation_suggestion=suggestion,
            )
            self._db.add(finding)

        # Step 4: Update run with results
        completed_at = datetime.now(UTC)
        run.completed_at = completed_at
        run.overall_status = report.overall_status
        run.score = report.score
        run.report = report.to_dict()

        await self._db.flush()

        log.info(
            "compliance.scheduler.run_completed",
            tenant_id=str(tenant_id),
            run_id=str(run.id),
            overall_status=report.overall_status,
            score=report.score,
            duration_ms=int(
                (completed_at - started_at).total_seconds() * 1000
            ),
        )

        # Step 5: Drift detection
        await self._detect_drift(tenant_id, run)

        return run

    async def get_latest_run(self, tenant_id: uuid.UUID) -> ComplianceRun | None:
        """Return the most recent completed compliance run for a tenant.

        Args:
            tenant_id: Tenant to query.

        Returns:
            Latest ComplianceRun or None if no runs exist.
        """
        result = await self._db.execute(
            select(ComplianceRun)
            .where(
                ComplianceRun.tenant_id == tenant_id,
                ComplianceRun.completed_at.isnot(None),
            )
            .order_by(ComplianceRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_runs(
        self,
        tenant_id: uuid.UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ComplianceRun]:
        """List compliance runs for a tenant, newest first.

        Args:
            tenant_id: Tenant to query.
            limit: Maximum number of runs to return.
            offset: Pagination offset.

        Returns:
            List of ComplianceRun objects.
        """
        result = await self._db.execute(
            select(ComplianceRun)
            .where(ComplianceRun.tenant_id == tenant_id)
            .order_by(ComplianceRun.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_run(self, run_id: uuid.UUID, tenant_id: uuid.UUID) -> ComplianceRun | None:
        """Fetch a specific compliance run, scoped to tenant.

        Args:
            run_id: Run UUID to fetch.
            tenant_id: Tenant scope (enforces isolation).

        Returns:
            ComplianceRun or None.
        """
        result = await self._db.execute(
            select(ComplianceRun).where(
                ComplianceRun.id == run_id,
                ComplianceRun.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _detect_drift(
        self, tenant_id: uuid.UUID, current_run: ComplianceRun
    ) -> None:
        """Compare current run with previous run and log drift alerts.

        Drift = any degradation in overall_status:
        - PASS → WARNING
        - PASS → FAIL
        - WARNING → FAIL

        Args:
            tenant_id: Tenant being evaluated.
            current_run: The just-completed run.
        """
        # Find the run immediately prior to this one
        result = await self._db.execute(
            select(ComplianceRun)
            .where(
                ComplianceRun.tenant_id == tenant_id,
                ComplianceRun.completed_at.isnot(None),
                ComplianceRun.id != current_run.id,
            )
            .order_by(ComplianceRun.started_at.desc())
            .limit(1)
        )
        previous_run: ComplianceRun | None = result.scalar_one_or_none()

        if previous_run is None:
            # First run ever – no drift to detect
            return

        prev_status = previous_run.overall_status
        curr_status = current_run.overall_status

        _degradation_map = {
            CheckStatus.PASS: 0,
            CheckStatus.WARNING: 1,
            CheckStatus.FAIL: 2,
            CheckStatus.SKIP: -1,
        }

        prev_severity = _degradation_map.get(prev_status, -1)
        curr_severity = _degradation_map.get(curr_status, -1)

        if curr_severity > prev_severity:
            log.warning(
                "compliance.scheduler.drift_detected",
                tenant_id=str(tenant_id),
                previous_run_id=str(previous_run.id),
                current_run_id=str(current_run.id),
                previous_status=prev_status,
                current_status=curr_status,
                previous_score=previous_run.score,
                current_score=current_run.score,
                score_delta=current_run.score - previous_run.score,
                alert=(
                    f"COMPLIANCE DRIFT: tenant {tenant_id} status changed "
                    f"from {prev_status} to {curr_status} "
                    f"(score: {previous_run.score} → {current_run.score})"
                ),
            )
        elif curr_severity < prev_severity:
            log.info(
                "compliance.scheduler.drift_improvement",
                tenant_id=str(tenant_id),
                previous_status=prev_status,
                current_status=curr_status,
                previous_score=previous_run.score,
                current_score=current_run.score,
            )
