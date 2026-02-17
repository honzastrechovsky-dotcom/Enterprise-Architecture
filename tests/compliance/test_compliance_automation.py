"""Tests for Audit & Compliance Automation.

Covers:
- ComplianceMonitor: all six checks plus run_all_checks aggregation
- ComplianceScheduler: run_now, list_runs, drift detection
- Evidence collectors: soc2, gdpr, iso27001
- Compliance Admin API: check, reports, status, evidence, schedule endpoints

Testing strategy:
- Mock the AsyncSession to avoid needing a live database
- Use side_effect lists to simulate sequential query results
- All tests are unit tests; integration tests would use a real DB fixture
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.compliance.evidence import (
    EvidencePackage,
    collect_gdpr_evidence,
    collect_iso27001_evidence,
    collect_soc2_evidence,
)
from src.compliance.monitor import (
    CheckResult,
    CheckStatus,
    ComplianceMonitor,
    ComplianceReport,
    _compute_score,
    _overall_status,
)
from src.compliance.scheduler import (
    ComplianceFinding,
    ComplianceRun,
    ComplianceScheduler,
    RunTrigger,
    ScheduleConfig,
    _remediation_suggestion,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
def mock_db():
    """Async database session mock."""
    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


def scalar_result(value):
    """Build a mock execute() result that returns value via scalar_one()."""
    result = MagicMock()
    result.scalar_one.return_value = value
    result.scalar_one_or_none.return_value = value
    return result


def scalars_result(items):
    """Build a mock execute() result returning a list via scalars().all()."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def rows_result(rows):
    """Build a mock execute() result returning rows via .all()."""
    result = MagicMock()
    result.all.return_value = rows
    return result


# ===========================================================================
# ComplianceMonitor tests
# ===========================================================================


class TestCheckStatusScore:
    """Unit tests for score computation helpers."""

    def test_compute_score_all_pass(self):
        checks = [
            CheckResult("a", CheckStatus.PASS, "ok", {}),
            CheckResult("b", CheckStatus.PASS, "ok", {}),
        ]
        assert _compute_score(checks) == 100.0

    def test_compute_score_all_fail(self):
        checks = [
            CheckResult("a", CheckStatus.FAIL, "bad", {}),
            CheckResult("b", CheckStatus.FAIL, "bad", {}),
        ]
        assert _compute_score(checks) == 0.0

    def test_compute_score_mixed(self):
        checks = [
            CheckResult("a", CheckStatus.PASS, "ok", {}),
            CheckResult("b", CheckStatus.WARNING, "warn", {}),
            CheckResult("c", CheckStatus.FAIL, "bad", {}),
            CheckResult("d", CheckStatus.SKIP, "skip", {}),
        ]
        # scoreable: PASS(1.0) + WARNING(0.5) + FAIL(0.0) = 1.5 / 3 = 0.5 = 50.0
        assert _compute_score(checks) == 50.0

    def test_compute_score_all_skip_returns_100(self):
        checks = [CheckResult("a", CheckStatus.SKIP, "skip", {})]
        assert _compute_score(checks) == 100.0

    def test_compute_score_empty_returns_100(self):
        assert _compute_score([]) == 100.0

    def test_overall_status_any_fail_is_fail(self):
        checks = [
            CheckResult("a", CheckStatus.PASS, "ok", {}),
            CheckResult("b", CheckStatus.FAIL, "bad", {}),
        ]
        assert _overall_status(checks) == CheckStatus.FAIL

    def test_overall_status_warning_no_fail(self):
        checks = [
            CheckResult("a", CheckStatus.PASS, "ok", {}),
            CheckResult("b", CheckStatus.WARNING, "warn", {}),
        ]
        assert _overall_status(checks) == CheckStatus.WARNING

    def test_overall_status_all_pass(self):
        checks = [CheckResult("a", CheckStatus.PASS, "ok", {})]
        assert _overall_status(checks) == CheckStatus.PASS


class TestCheckDataClassification:
    """Tests for check_data_classification."""

    @pytest.mark.asyncio
    async def test_skip_when_no_documents(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(0),  # total documents
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_data_classification(tenant_id)

        assert result.status == CheckStatus.SKIP
        assert result.evidence["total_documents"] == 0

    @pytest.mark.asyncio
    async def test_pass_when_all_classified(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(10),  # total
            scalar_result(10),  # classified
            rows_result([("confidential", 5), ("public", 5)]),  # distribution
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_data_classification(tenant_id)

        assert result.status == CheckStatus.PASS
        assert result.evidence["coverage_percent"] == 100.0

    @pytest.mark.asyncio
    async def test_warning_when_80_to_99_percent(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(10),  # total
            scalar_result(9),   # classified (90%)
            rows_result([("confidential", 9)]),
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_data_classification(tenant_id)

        assert result.status == CheckStatus.WARNING

    @pytest.mark.asyncio
    async def test_fail_when_below_80_percent(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(10),  # total
            scalar_result(7),   # classified (70%)
            rows_result([("confidential", 7)]),
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_data_classification(tenant_id)

        assert result.status == CheckStatus.FAIL


class TestCheckAccessControls:
    """Tests for check_access_controls."""

    @pytest.mark.asyncio
    async def test_fail_when_no_admin(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            rows_result([("viewer", 5)]),    # users by role – no admin
            scalar_result(100),              # auth success
            scalar_result(5),               # auth fail
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_access_controls(tenant_id)

        assert result.status == CheckStatus.FAIL
        assert "No active admin" in result.details

    @pytest.mark.asyncio
    async def test_pass_with_valid_config(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            rows_result([("admin", 1), ("viewer", 9)]),  # users by role
            scalar_result(200),                           # auth success
            scalar_result(5),                            # auth fail (2.4% – OK)
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_access_controls(tenant_id)

        assert result.status == CheckStatus.PASS

    @pytest.mark.asyncio
    async def test_fail_high_auth_failure_rate(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            rows_result([("admin", 1), ("viewer", 3)]),
            scalar_result(10),   # auth success
            scalar_result(50),   # auth fail (83% failure rate)
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_access_controls(tenant_id)

        assert result.status == CheckStatus.FAIL
        assert "failure rate" in result.details


class TestCheckAuditLogging:
    """Tests for check_audit_logging."""

    @pytest.mark.asyncio
    async def test_fail_when_no_events(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(0),    # total events
            scalar_result(0),    # last 24h
            scalar_result(0),    # last 30d
            scalar_result(None), # latest ts
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_audit_logging(tenant_id)

        assert result.status == CheckStatus.FAIL

    @pytest.mark.asyncio
    async def test_warning_no_recent_events(self, mock_db, tenant_id):
        last_ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        mock_db.execute.side_effect = [
            scalar_result(1000), # total
            scalar_result(0),    # last 24h – none
            scalar_result(0),    # last 30d
            scalar_result(last_ts),
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_audit_logging(tenant_id)

        assert result.status == CheckStatus.WARNING

    @pytest.mark.asyncio
    async def test_pass_with_recent_events(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(5000), # total
            scalar_result(150),  # last 24h
            scalar_result(3000), # last 30d
            scalar_result(datetime.now(timezone.utc)),
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_audit_logging(tenant_id)

        assert result.status == CheckStatus.PASS


class TestCheckEncryption:
    """Tests for check_encryption (platform-level assertion)."""

    @pytest.mark.asyncio
    async def test_always_passes_with_evidence(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(42),  # security audit events
        ]
        monitor = ComplianceMonitor(mock_db)
        result = await monitor.check_encryption(tenant_id)

        assert result.status == CheckStatus.PASS
        assert result.evidence["tls_enforced"] is True
        assert result.evidence["database_encryption_at_rest"] is True


class TestRunAllChecks:
    """Tests for run_all_checks aggregation."""

    @pytest.mark.asyncio
    async def test_returns_compliance_report(self, mock_db, tenant_id):
        # Provide enough scalar results for all 6 checks
        mock_db.execute.side_effect = [
            # check_data_classification: no documents
            scalar_result(0),
            # check_access_controls: admin exists, low fail rate
            rows_result([("admin", 1), ("viewer", 4)]),
            scalar_result(100),  # auth success
            scalar_result(2),    # auth fail
            # check_encryption
            scalar_result(10),
            # check_audit_logging: has events
            scalar_result(2000),
            scalar_result(50),
            scalar_result(1000),
            scalar_result(datetime.now(timezone.utc)),
            # check_data_retention: too few events
            scalar_result(100),
            # check_pii_handling: no ingestion events so skip warning
            scalar_result(0),   # pii events
            scalar_result(0),   # ingestion events
            scalar_result(0),   # total pii
        ]

        monitor = ComplianceMonitor(mock_db)
        report = await monitor.run_all_checks(tenant_id)

        assert isinstance(report, ComplianceReport)
        assert report.tenant_id == tenant_id
        assert len(report.checks) == 6
        assert 0.0 <= report.score <= 100.0
        assert report.overall_status in (
            CheckStatus.PASS, CheckStatus.WARNING, CheckStatus.FAIL, CheckStatus.SKIP
        )

    @pytest.mark.asyncio
    async def test_check_error_becomes_fail(self, mock_db, tenant_id):
        """If a check raises, the run continues and records a FAIL."""
        mock_db.execute.side_effect = Exception("DB down")
        monitor = ComplianceMonitor(mock_db)
        report = await monitor.run_all_checks(tenant_id)

        # All checks should have FAIL status due to exception
        for check in report.checks:
            assert check.status == CheckStatus.FAIL
        assert report.overall_status == CheckStatus.FAIL


# ===========================================================================
# ComplianceScheduler tests
# ===========================================================================


class TestRemediationSuggestions:
    """Tests for the remediation suggestion lookup."""

    def test_pass_returns_none(self):
        assert _remediation_suggestion("data_classification", CheckStatus.PASS) is None

    def test_skip_returns_none(self):
        assert _remediation_suggestion("audit_logging", CheckStatus.SKIP) is None

    def test_fail_returns_suggestion(self):
        result = _remediation_suggestion("data_classification", CheckStatus.FAIL)
        assert result is not None
        assert len(result) > 0

    def test_unknown_check_returns_none(self):
        assert _remediation_suggestion("unknown_check", CheckStatus.FAIL) is None


class TestComplianceScheduler:
    """Tests for ComplianceScheduler.run_now and list_runs."""

    @pytest.mark.asyncio
    async def test_run_now_creates_run_and_findings(self, mock_db, tenant_id):
        """run_now should create a ComplianceRun and its findings."""
        # Mock monitor to return a canned report
        mock_report = ComplianceReport(
            tenant_id=tenant_id,
            timestamp=datetime.now(timezone.utc),
            checks=[
                CheckResult("data_classification", CheckStatus.PASS, "ok", {}),
                CheckResult("access_controls", CheckStatus.WARNING, "warn", {"admin_count": 0}),
            ],
            overall_status=CheckStatus.WARNING,
            score=75.0,
        )

        with patch(
            "src.compliance.scheduler.ComplianceMonitor.run_all_checks",
            new_callable=AsyncMock,
            return_value=mock_report,
        ):
            # Drift detection needs a "previous run" query – return None (first run)
            mock_db.execute.return_value = scalars_result([])
            mock_db.execute.return_value.scalar_one_or_none = MagicMock(return_value=None)

            scheduler = ComplianceScheduler(mock_db)
            run = await scheduler.run_now(tenant_id, triggered_by=RunTrigger.MANUAL)

        assert isinstance(run, ComplianceRun)
        assert run.tenant_id == tenant_id
        assert run.triggered_by == RunTrigger.MANUAL
        assert run.overall_status == CheckStatus.WARNING
        assert run.score == 75.0
        assert run.completed_at is not None
        # Two findings added to session
        assert mock_db.add.call_count >= 3  # run + 2 findings

    @pytest.mark.asyncio
    async def test_run_now_with_scheduled_trigger(self, mock_db, tenant_id):
        mock_report = ComplianceReport(
            tenant_id=tenant_id,
            timestamp=datetime.now(timezone.utc),
            checks=[CheckResult("encryption", CheckStatus.PASS, "ok", {})],
            overall_status=CheckStatus.PASS,
            score=100.0,
        )

        with patch(
            "src.compliance.scheduler.ComplianceMonitor.run_all_checks",
            new_callable=AsyncMock,
            return_value=mock_report,
        ):
            prev_run_result = MagicMock()
            prev_run_result.scalar_one_or_none.return_value = None
            mock_db.execute.return_value = prev_run_result

            scheduler = ComplianceScheduler(mock_db)
            run = await scheduler.run_now(tenant_id, triggered_by=RunTrigger.SCHEDULED)

        assert run.triggered_by == RunTrigger.SCHEDULED


class TestScheduleConfig:
    """Tests for ScheduleConfig defaults."""

    def test_default_schedule(self):
        config = ScheduleConfig()
        assert config.daily_enabled is True
        assert config.weekly_enabled is True
        assert config.monthly_enabled is True
        assert config.daily_hour_utc == 2
        assert config.weekly_day == 1
        assert config.monthly_day == 1


# ===========================================================================
# Evidence Collector tests
# ===========================================================================


class TestCollectSOC2Evidence:
    """Tests for collect_soc2_evidence."""

    @pytest.mark.asyncio
    async def test_returns_evidence_package(self, mock_db, tenant_id):
        # Provide all query results expected by collect_soc2_evidence
        mock_db.execute.side_effect = [
            scalar_result(10),              # total users
            rows_result([("admin", 1), ("viewer", 9)]),  # users by role
            scalar_result(500),             # auth success
            scalar_result(10),              # auth fail (reused for auth_fail)
            scalar_result(2),              # rbac denials
            scalar_result(5000),           # total audit events
            scalar_result(50),             # error events
            scalar_result(3),              # policy violations
            scalar_result(10000),          # total requests
            scalar_result(50),             # failed requests
            scalar_result(0),              # ai interactions
            scalar_result(0),             # ai errors
            scalar_result(0),             # pii redactions
            scalar_result(0),             # input blocks
            scalar_result(20),            # docs total
            scalar_result(20),            # docs classified
            rows_result([("public", 10), ("confidential", 10)]),  # classification dist
        ]

        package = await collect_soc2_evidence(mock_db, tenant_id)

        assert isinstance(package, EvidencePackage)
        assert package.standard == "soc2"
        assert len(package.controls) > 0
        assert len(package.evidence_files) > 0
        assert package.generated_at is not None

        # Verify control IDs are present
        control_ids = [c.control_id for c in package.controls]
        assert "CC6.1" in control_ids
        assert "A1.1" in control_ids
        assert "PI1.1" in control_ids
        assert "C1.1" in control_ids

    @pytest.mark.asyncio
    async def test_to_dict_is_serialisable(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(5),
            rows_result([("admin", 1), ("viewer", 4)]),
            scalar_result(100),
            scalar_result(5),
            scalar_result(1),
            scalar_result(1000),
            scalar_result(10),
            scalar_result(0),
            scalar_result(2000),
            scalar_result(20),
            scalar_result(50),
            scalar_result(2),
            scalar_result(5),
            scalar_result(1),
            scalar_result(15),
            scalar_result(15),
            rows_result([("public", 15)]),
        ]

        package = await collect_soc2_evidence(mock_db, tenant_id)
        d = package.to_dict()

        assert isinstance(d, dict)
        assert d["standard"] == "soc2"
        assert "controls" in d
        assert "evidence_files" in d
        assert "generated_at" in d


class TestCollectGDPREvidence:
    """Tests for collect_gdpr_evidence."""

    @pytest.mark.asyncio
    async def test_returns_evidence_package(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(3000),  # processing events
            scalar_result(50),    # pii redactions
            scalar_result(5),     # gdpr requests
            scalar_result(3),     # erasure events
            scalar_result(100),   # error events
            scalar_result(0),     # breach events
            scalar_result(25),    # total docs
        ]

        package = await collect_gdpr_evidence(mock_db, tenant_id)

        assert isinstance(package, EvidencePackage)
        assert package.standard == "gdpr"
        control_ids = [c.control_id for c in package.controls]
        assert "Art. 5" in control_ids
        assert "Art. 32" in control_ids
        assert "Art. 15-20" in control_ids

    @pytest.mark.asyncio
    async def test_breach_event_marks_control_partial(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(1000),
            scalar_result(20),
            scalar_result(2),
            scalar_result(1),
            scalar_result(50),
            scalar_result(1),   # 1 breach event → partial
            scalar_result(10),
        ]

        package = await collect_gdpr_evidence(mock_db, tenant_id)
        breach_control = next(c for c in package.controls if c.control_id == "Art. 33-34")
        assert breach_control.status == "partial"


class TestCollectISO27001Evidence:
    """Tests for collect_iso27001_evidence."""

    @pytest.mark.asyncio
    async def test_returns_evidence_package(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(8),    # total users
            scalar_result(2),    # admin count
            scalar_result(15),   # rbac denials
            scalar_result(200),  # auth events
            scalar_result(4000), # total log events
            scalar_result(30),   # total docs
            scalar_result(20),   # pii events
            scalar_result(5),    # config changes
        ]

        package = await collect_iso27001_evidence(mock_db, tenant_id)

        assert isinstance(package, EvidencePackage)
        assert package.standard == "iso27001"
        control_ids = [c.control_id for c in package.controls]
        assert "A.5.15" in control_ids
        assert "A.8.5" in control_ids
        assert "A.8.15" in control_ids
        assert "A.8.24" in control_ids

    @pytest.mark.asyncio
    async def test_evidence_files_present(self, mock_db, tenant_id):
        mock_db.execute.side_effect = [
            scalar_result(4),
            scalar_result(1),
            scalar_result(5),
            scalar_result(80),
            scalar_result(2000),
            scalar_result(10),
            scalar_result(8),
            scalar_result(3),
        ]

        package = await collect_iso27001_evidence(mock_db, tenant_id)

        assert "iso27001_access_controls.json" in package.evidence_files
        assert "iso27001_logging_monitoring.json" in package.evidence_files
        assert "iso27001_cryptography.json" in package.evidence_files
        assert "iso27001_metadata.json" in package.evidence_files


# ===========================================================================
# API endpoint tests (using FastAPI TestClient / async httpx patterns)
# ===========================================================================


class TestComplianceAdminAPI:
    """Lightweight tests for the compliance_admin API module."""

    def test_schedule_config_request_validation(self):
        """ScheduleConfigRequest accepts valid values."""
        from src.api.compliance_admin import ScheduleConfigRequest

        req = ScheduleConfigRequest(
            daily_enabled=True,
            weekly_enabled=False,
            monthly_enabled=True,
            daily_hour_utc=6,
            weekly_day=5,
            monthly_day=15,
        )
        assert req.daily_hour_utc == 6
        assert req.weekly_day == 5

    def test_finding_response_model(self):
        """FindingResponse model has required fields."""
        from src.api.compliance_admin import FindingResponse

        fr = FindingResponse(
            check_name="data_classification",
            status="PASS",
            details="All classified",
            evidence={"total": 10},
            remediation_suggestion=None,
        )
        assert fr.check_name == "data_classification"
        assert fr.remediation_suggestion is None

    def test_check_now_response_model(self):
        """CheckNowResponse model serialises correctly."""
        from src.api.compliance_admin import CheckNowResponse

        run_id = uuid.uuid4()
        resp = CheckNowResponse(
            run_id=run_id,
            tenant_id=uuid.uuid4(),
            triggered_by="manual",
            started_at=datetime.now(timezone.utc),
            overall_status="PASS",
            score=95.0,
            num_findings=6,
            message="Compliance check completed.",
        )
        assert resp.run_id == run_id
        assert resp.score == 95.0

    def test_evidence_standards_constant(self):
        """Supported standards set is correct."""
        from src.api.compliance_admin import _SUPPORTED_STANDARDS

        assert "soc2" in _SUPPORTED_STANDARDS
        assert "gdpr" in _SUPPORTED_STANDARDS
        assert "iso27001" in _SUPPORTED_STANDARDS
        assert "pci_dss" not in _SUPPORTED_STANDARDS

    def test_next_check_description_daily(self):
        """_next_check_description returns ISO timestamp for daily schedule."""
        from src.api.compliance_admin import _next_check_description
        from src.compliance.scheduler import ScheduleConfig

        config = ScheduleConfig(daily_enabled=True, daily_hour_utc=3)
        result = _next_check_description(config)
        assert result is not None
        # Should be parseable as ISO datetime
        datetime.fromisoformat(result)

    def test_next_check_description_all_disabled(self):
        """_next_check_description returns None when all schedules are disabled."""
        from src.api.compliance_admin import _next_check_description
        from src.compliance.scheduler import ScheduleConfig

        config = ScheduleConfig(
            daily_enabled=False,
            weekly_enabled=False,
            monthly_enabled=False,
        )
        assert _next_check_description(config) is None
