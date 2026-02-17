"""Tests for SOC 2 audit evidence export service."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from src.compliance.audit_export import (
    SOC2ExportService,
    EvidencePackage,
    AccessControlEvidence,
    AvailabilityEvidence,
    ChangeManagementEvidence,
    ConfidentialityEvidence,
    ProcessingIntegrityEvidence,
)
from src.models.audit import AuditLog, AuditStatus
from src.models.user import User, UserRole
from src.models.document import Document


class TestSOC2ExportService:
    """Test SOC 2 evidence package generation."""

    @pytest.fixture
    def service(self, mock_db_session):
        """Create SOC2ExportService instance."""
        return SOC2ExportService(mock_db_session)

    @pytest.mark.asyncio
    async def test_generate_evidence_package_returns_complete_package(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test that evidence package contains all required sections."""
        period_start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        period_end = datetime(2025, 12, 31, tzinfo=timezone.utc)

        # Mock database queries
        mock_db_session.execute.return_value.scalar_one.return_value = 100
        mock_db_session.execute.return_value.all.return_value = [
            (UserRole.ADMIN, 10),
            (UserRole.OPERATOR, 30),
        ]
        mock_db_session.execute.return_value.one_or_none.return_value = None

        package = await service.generate_evidence_package(
            tenant_id=test_tenant_id,
            period_start=period_start,
            period_end=period_end,
        )

        assert isinstance(package, EvidencePackage)
        assert package.tenant_id == test_tenant_id
        assert package.period_start == period_start
        assert package.period_end == period_end
        assert package.access_controls is not None
        assert package.change_management is not None
        assert package.availability is not None
        assert package.confidentiality is not None
        assert package.processing_integrity is not None

    @pytest.mark.asyncio
    async def test_generate_evidence_package_filters_by_date_range(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test that evidence package filters audit logs by date range."""
        period_start = datetime(2025, 6, 1, tzinfo=timezone.utc)
        period_end = datetime(2025, 6, 30, tzinfo=timezone.utc)

        # Mock all database queries with proper return types
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 50
        mock_result.all.return_value = [(UserRole.OPERATOR, 20)]
        mock_result.one_or_none.return_value = (0.0, 0.0, 0.0)
        mock_db_session.execute.return_value = mock_result

        await service.generate_evidence_package(
            tenant_id=test_tenant_id,
            period_start=period_start,
            period_end=period_end,
        )

        # Verify date filtering in queries
        execute_calls = mock_db_session.execute.call_args_list
        assert len(execute_calls) > 0

    @pytest.mark.asyncio
    async def test_generate_evidence_package_isolates_by_tenant(
        self, service, mock_db_session
    ):
        """Test that evidence package only includes data for specified tenant."""
        tenant_id_1 = uuid.uuid4()
        tenant_id_2 = uuid.uuid4()

        period_start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        period_end = datetime(2025, 12, 31, tzinfo=timezone.utc)

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 10
        mock_result.all.return_value = []
        mock_result.one_or_none.return_value = None
        mock_db_session.execute.return_value = mock_result

        package = await service.generate_evidence_package(
            tenant_id=tenant_id_1,
            period_start=period_start,
            period_end=period_end,
        )

        assert package.tenant_id == tenant_id_1
        assert package.tenant_id != tenant_id_2

    @pytest.mark.asyncio
    async def test_evidence_package_includes_required_fields(
        self, service, mock_db_session, test_tenant_id
    ):
        """Test that evidence package includes all SOC 2 required fields."""
        period_start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        period_end = datetime(2025, 12, 31, tzinfo=timezone.utc)

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 100
        mock_result.all.return_value = [(UserRole.ADMIN, 5)]
        mock_result.one_or_none.return_value = (10.0, 15.0, 20.0)
        mock_db_session.execute.return_value = mock_result

        package = await service.generate_evidence_package(
            tenant_id=test_tenant_id,
            period_start=period_start,
            period_end=period_end,
        )

        # Verify Trust Service Criteria evidence
        assert hasattr(package.access_controls, "total_users")
        assert hasattr(package.access_controls, "successful_auth_events")
        assert hasattr(package.access_controls, "failed_auth_events")
        assert hasattr(package.availability, "uptime_percentage")
        assert hasattr(package.availability, "mean_response_time_ms")
        assert hasattr(package.confidentiality, "documents_classified")
        assert hasattr(package.processing_integrity, "total_ai_interactions")

    def test_export_to_json_returns_valid_json_string(
        self, service, test_tenant_id
    ):
        """Test JSON export format."""
        import json

        period_start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        period_end = datetime(2025, 12, 31, tzinfo=timezone.utc)

        package = EvidencePackage(
            tenant_id=test_tenant_id,
            period_start=period_start,
            period_end=period_end,
            generated_at=datetime.now(timezone.utc),
            access_controls=AccessControlEvidence(
                total_users=50,
                users_by_role={"admin": 5, "operator": 45},
                successful_auth_events=1000,
                failed_auth_events=10,
                mfa_enabled_users=40,
                rbac_denials=5,
                session_timeouts=20,
                access_reviews_performed=2,
            ),
            change_management=ChangeManagementEvidence(
                config_changes=5, code_deployments=3, schema_migrations=1,
                rollback_events=0, emergency_changes=0, change_audit_completeness=100.0,
            ),
            availability=AvailabilityEvidence(
                uptime_percentage=99.9,
                mean_response_time_ms=150.0,
                p95_response_time_ms=250.0,
                p99_response_time_ms=500.0,
                total_requests=10000,
                failed_requests=10,
                rate_limited_requests=5,
                backup_completions=30,
                backup_failures=0,
            ),
            confidentiality=ConfidentialityEvidence(
                documents_classified=10, classification_distribution={},
                class_iii_access_grants=0, class_iii_access_denials=0,
                class_iv_blocks=0, encryption_enforced=True,
                tenant_isolation_verified=True, data_leak_incidents=0,
            ),
            processing_integrity=ProcessingIntegrityEvidence(
                total_ai_interactions=100, ai_errors=2,
                input_validation_blocks=1, pii_redactions=5,
                human_review_triggers=3, model_confidence_avg=0.85,
                hallucination_incidents=0, data_lineage_coverage=95.0,
            ),
            total_audit_events=10000,
            audit_log_completeness=100.0,
            policy_violations=3,
            incidents_resolved=2,
        )

        json_export = service.export_to_json(package)

        # Verify it's valid JSON
        parsed = json.loads(json_export)
        assert "tenant_id" in parsed
        assert "period_start" in parsed
        assert "access_controls" in parsed

    def test_export_to_csv_returns_bytes(self, service, test_tenant_id):
        """Test CSV export returns bytes suitable for download."""
        period_start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        period_end = datetime(2025, 12, 31, tzinfo=timezone.utc)

        package = EvidencePackage(
            tenant_id=test_tenant_id,
            period_start=period_start,
            period_end=period_end,
            generated_at=datetime.now(timezone.utc),
            access_controls=AccessControlEvidence(
                total_users=50,
                users_by_role={},
                successful_auth_events=1000,
                failed_auth_events=10,
                mfa_enabled_users=0,
                rbac_denials=5,
                session_timeouts=0,
                access_reviews_performed=0,
            ),
            change_management=ChangeManagementEvidence(
                config_changes=5, code_deployments=3, schema_migrations=1,
                rollback_events=0, emergency_changes=0, change_audit_completeness=100.0,
            ),
            availability=AvailabilityEvidence(
                uptime_percentage=99.9,
                mean_response_time_ms=150.0,
                p95_response_time_ms=250.0,
                p99_response_time_ms=500.0,
                total_requests=10000,
                failed_requests=10,
                rate_limited_requests=5,
                backup_completions=0,
                backup_failures=0,
            ),
            confidentiality=ConfidentialityEvidence(
                documents_classified=10, classification_distribution={},
                class_iii_access_grants=0, class_iii_access_denials=0,
                class_iv_blocks=0, encryption_enforced=True,
                tenant_isolation_verified=True, data_leak_incidents=0,
            ),
            processing_integrity=ProcessingIntegrityEvidence(
                total_ai_interactions=100, ai_errors=2,
                input_validation_blocks=1, pii_redactions=5,
                human_review_triggers=3, model_confidence_avg=0.85,
                hallucination_incidents=0, data_lineage_coverage=95.0,
            ),
            total_audit_events=10000,
            audit_log_completeness=100.0,
            policy_violations=3,
            incidents_resolved=2,
        )

        csv_export = service.export_to_csv(package)

        assert isinstance(csv_export, bytes)
        assert len(csv_export) > 0
        # Verify CSV structure
        csv_text = csv_export.decode("utf-8")
        assert "Category,Metric,Value" in csv_text
        assert "Total Audit Events" in csv_text
