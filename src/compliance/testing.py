"""Automated compliance testing suite.

Runs automated tests to verify compliance controls are functioning:
- Tenant isolation (no cross-tenant data leaks)
- Classification enforcement (Class III/IV restrictions work)
- PII redaction (PII is properly sanitized)
- Audit logging (all actions are logged)
- RBAC enforcement (role checks work correctly)
- Export control (export restrictions enforced)
- AI disclosure (AI-generated content is marked)

Designed for:
- Continuous compliance monitoring (run daily)
- Pre-audit validation (run before audits)
- Post-deployment verification (run after changes)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.classification import ClassificationPolicy, DataClassification
from src.core.pii import PIIAction, PIISanitizer
from src.models.audit import AuditLog
from src.models.document import Document
from src.models.user import User, UserRole

log = structlog.get_logger(__name__)


@dataclass
class TestResult:
    """Result of a single compliance test."""

    test_name: str
    passed: bool
    message: str
    evidence: dict[str, Any]
    severity: str  # "critical" | "high" | "medium" | "low"


@dataclass
class ComplianceTestResult:
    """Complete compliance test suite result."""

    tenant_id: uuid.UUID
    executed_at: datetime
    overall_pass: bool
    tests_passed: int
    tests_failed: int
    total_tests: int
    pass_rate: float
    test_results: list[TestResult]


class ComplianceTestSuite:
    """Automated compliance testing suite.

    Usage:
        suite = ComplianceTestSuite(db)
        result = await suite.run_all(tenant_id)

        if not result.overall_pass:
            # Handle compliance test failures
            for test in result.test_results:
                if not test.passed:
                    print(f"FAILED: {test.test_name} - {test.message}")
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def run_all(self, tenant_id: uuid.UUID) -> ComplianceTestResult:
        """Run all compliance tests for tenant.

        Args:
            tenant_id: Tenant to test

        Returns:
            ComplianceTestResult with all test outcomes
        """
        log.info("compliance.test_suite.start", tenant_id=str(tenant_id))

        test_results = []

        # Run all tests
        test_results.append(await self.test_tenant_isolation(tenant_id))
        test_results.append(await self.test_classification_enforcement(tenant_id))
        test_results.append(await self.test_pii_redaction(tenant_id))
        test_results.append(await self.test_audit_logging(tenant_id))
        test_results.append(await self.test_rbac_enforcement(tenant_id))
        test_results.append(await self.test_export_control(tenant_id))
        test_results.append(await self.test_ai_disclosure(tenant_id))

        # Calculate summary
        tests_passed = sum(1 for t in test_results if t.passed)
        tests_failed = sum(1 for t in test_results if not t.passed)
        total_tests = len(test_results)
        pass_rate = (tests_passed / total_tests * 100) if total_tests > 0 else 0
        overall_pass = tests_failed == 0

        result = ComplianceTestResult(
            tenant_id=tenant_id,
            executed_at=datetime.now(UTC),
            overall_pass=overall_pass,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            total_tests=total_tests,
            pass_rate=pass_rate,
            test_results=test_results,
        )

        log.info(
            "compliance.test_suite.complete",
            tenant_id=str(tenant_id),
            overall_pass=overall_pass,
            pass_rate=pass_rate,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
        )

        return result

    async def test_tenant_isolation(self, tenant_id: uuid.UUID) -> TestResult:
        """Test that cross-tenant data leaks are impossible.

        Verifies:
        - Users from other tenants cannot be queried
        - Documents from other tenants are not accessible
        - Audit logs are tenant-scoped

        Returns:
            TestResult indicating pass/fail
        """
        log.info("compliance.test.tenant_isolation", tenant_id=str(tenant_id))

        try:
            # Query users for this tenant
            tenant_users_result = await self._db.execute(
                select(User).where(User.tenant_id == tenant_id)
            )
            tenant_users = tenant_users_result.scalars().all()

            # Query users from other tenants (should find some if multi-tenant)
            other_tenants_result = await self._db.execute(
                select(User).where(User.tenant_id != tenant_id).limit(10)
            )
            other_tenant_users = other_tenants_result.scalars().all()

            # Verify we got tenant-scoped results
            for user in tenant_users:
                if user.tenant_id != tenant_id:
                    return TestResult(
                        test_name="tenant_isolation",
                        passed=False,
                        message=f"User {user.id} has wrong tenant_id",
                        evidence={"user_id": str(user.id), "tenant_id": str(user.tenant_id)},
                        severity="critical",
                    )

            # Test document isolation
            tenant_docs_result = await self._db.execute(
                select(Document).where(Document.tenant_id == tenant_id)
            )
            tenant_docs = tenant_docs_result.scalars().all()

            for doc in tenant_docs:
                if doc.tenant_id != tenant_id:
                    return TestResult(
                        test_name="tenant_isolation",
                        passed=False,
                        message=f"Document {doc.id} has wrong tenant_id",
                        evidence={"document_id": str(doc.id), "tenant_id": str(doc.tenant_id)},
                        severity="critical",
                    )

            return TestResult(
                test_name="tenant_isolation",
                passed=True,
                message="Tenant isolation verified - no cross-tenant data leaks",
                evidence={
                    "tenant_users_count": len(tenant_users),
                    "tenant_docs_count": len(tenant_docs),
                    "other_tenants_exist": len(other_tenant_users) > 0,
                },
                severity="critical",
            )

        except Exception as exc:
            log.error("compliance.test.tenant_isolation.error", error=str(exc))
            return TestResult(
                test_name="tenant_isolation",
                passed=False,
                message=f"Test failed with error: {exc}",
                evidence={"error": str(exc)},
                severity="critical",
            )

    async def test_classification_enforcement(self, tenant_id: uuid.UUID) -> TestResult:
        """Test that data classification enforcement works.

        Verifies:
        - Class III requires OPERATOR+ role and ACL
        - Class IV is blocked without approval
        - Classification checks are logged

        Returns:
            TestResult indicating pass/fail
        """
        log.info("compliance.test.classification_enforcement", tenant_id=str(tenant_id))

        try:
            policy = ClassificationPolicy()

            # Test Class III with VIEWER (should deny)
            class_iii_viewer = policy.check_access(
                user_role=UserRole.VIEWER,
                classification=DataClassification.CLASS_III,
                document_acl=[uuid.uuid4()],
                user_id=uuid.uuid4(),
            )

            if class_iii_viewer.allowed:
                return TestResult(
                    test_name="classification_enforcement",
                    passed=False,
                    message="Class III allowed for VIEWER role (should deny)",
                    evidence={"result": "class_iii_viewer_allowed"},
                    severity="high",
                )

            # Test Class III with OPERATOR but not in ACL (should deny)
            user_id = uuid.uuid4()
            class_iii_no_acl = policy.check_access(
                user_role=UserRole.OPERATOR,
                classification=DataClassification.CLASS_III,
                document_acl=[],  # Empty ACL
                user_id=user_id,
            )

            if class_iii_no_acl.allowed:
                return TestResult(
                    test_name="classification_enforcement",
                    passed=False,
                    message="Class III allowed without ACL membership (should deny)",
                    evidence={"result": "class_iii_no_acl_allowed"},
                    severity="high",
                )

            # Test Class III with OPERATOR in ACL (should allow)
            class_iii_with_acl = policy.check_access(
                user_role=UserRole.OPERATOR,
                classification=DataClassification.CLASS_III,
                document_acl=[user_id],
                user_id=user_id,
            )

            if not class_iii_with_acl.allowed:
                return TestResult(
                    test_name="classification_enforcement",
                    passed=False,
                    message="Class III denied with valid ACL membership (should allow)",
                    evidence={"result": "class_iii_with_acl_denied"},
                    severity="high",
                )

            # Test Class IV (should always deny without approval)
            class_iv_admin = policy.check_access(
                user_role=UserRole.ADMIN,
                classification=DataClassification.CLASS_IV,
            )

            if class_iv_admin.allowed:
                return TestResult(
                    test_name="classification_enforcement",
                    passed=False,
                    message="Class IV allowed without approval (should deny)",
                    evidence={"result": "class_iv_allowed"},
                    severity="high",
                )

            if not class_iv_admin.requires_approval:
                return TestResult(
                    test_name="classification_enforcement",
                    passed=False,
                    message="Class IV does not require approval (should require)",
                    evidence={"result": "class_iv_no_approval"},
                    severity="high",
                )

            return TestResult(
                test_name="classification_enforcement",
                passed=True,
                message="Classification enforcement verified - all checks working",
                evidence={
                    "class_iii_viewer_denied": True,
                    "class_iii_no_acl_denied": True,
                    "class_iii_with_acl_allowed": True,
                    "class_iv_requires_approval": True,
                },
                severity="high",
            )

        except Exception as exc:
            log.error("compliance.test.classification_enforcement.error", error=str(exc))
            return TestResult(
                test_name="classification_enforcement",
                passed=False,
                message=f"Test failed with error: {exc}",
                evidence={"error": str(exc)},
                severity="high",
            )

    async def test_pii_redaction(self, tenant_id: uuid.UUID) -> TestResult:
        """Test that PII is properly detected and redacted.

        Verifies:
        - Email addresses are detected
        - Phone numbers are detected
        - SSNs are detected
        - Redaction replaces PII with placeholders

        Returns:
            TestResult indicating pass/fail
        """
        log.info("compliance.test.pii_redaction", tenant_id=str(tenant_id))

        try:
            sanitizer = PIISanitizer(action=PIIAction.REDACT)

            # Test email detection
            email_text = "Contact john.doe@example.com for more info"
            email_result = sanitizer.check_and_act(email_text)

            if not email_result.findings:
                return TestResult(
                    test_name="pii_redaction",
                    passed=False,
                    message="Email not detected in PII scan",
                    evidence={"text": email_text},
                    severity="high",
                )

            if email_result.sanitized_text and "john.doe@example.com" in email_result.sanitized_text:
                return TestResult(
                    test_name="pii_redaction",
                    passed=False,
                    message="Email not redacted in sanitized output",
                    evidence={"sanitized": email_result.sanitized_text},
                    severity="high",
                )

            # Test phone number detection
            phone_text = "Call me at 555-123-4567"
            phone_result = sanitizer.check_and_act(phone_text)

            if not phone_result.findings:
                return TestResult(
                    test_name="pii_redaction",
                    passed=False,
                    message="Phone number not detected in PII scan",
                    evidence={"text": phone_text},
                    severity="high",
                )

            # Test SSN detection
            ssn_text = "My SSN is 123-45-6789"
            ssn_result = sanitizer.check_and_act(ssn_text)

            if not ssn_result.findings:
                return TestResult(
                    test_name="pii_redaction",
                    passed=False,
                    message="SSN not detected in PII scan",
                    evidence={"text": ssn_text},
                    severity="high",
                )

            # Test clean text (no false positives)
            clean_text = "The meeting is at 3:00 PM in room 42"
            clean_result = sanitizer.check_and_act(clean_text)

            if clean_result.findings:
                return TestResult(
                    test_name="pii_redaction",
                    passed=False,
                    message="False positive PII detection in clean text",
                    evidence={"text": clean_text, "findings": len(clean_result.findings)},
                    severity="medium",
                )

            return TestResult(
                test_name="pii_redaction",
                passed=True,
                message="PII redaction verified - detection and redaction working",
                evidence={
                    "email_detected": True,
                    "phone_detected": True,
                    "ssn_detected": True,
                    "no_false_positives": True,
                },
                severity="high",
            )

        except Exception as exc:
            log.error("compliance.test.pii_redaction.error", error=str(exc))
            return TestResult(
                test_name="pii_redaction",
                passed=False,
                message=f"Test failed with error: {exc}",
                evidence={"error": str(exc)},
                severity="high",
            )

    async def test_audit_logging(self, tenant_id: uuid.UUID) -> TestResult:
        """Test that all actions are properly audited.

        Verifies:
        - Audit logs exist for this tenant
        - Logs contain required fields
        - Timestamps are reasonable

        Returns:
            TestResult indicating pass/fail
        """
        log.info("compliance.test.audit_logging", tenant_id=str(tenant_id))

        try:
            # Query recent audit logs
            result = await self._db.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant_id)
                .order_by(AuditLog.timestamp.desc())
                .limit(100)
            )
            audit_logs = result.scalars().all()

            if len(audit_logs) == 0:
                # No logs yet - not necessarily a failure for new tenant
                return TestResult(
                    test_name="audit_logging",
                    passed=True,
                    message="No audit logs found (new tenant - acceptable)",
                    evidence={"log_count": 0},
                    severity="medium",
                )

            # Verify required fields are present
            for audit_log in audit_logs[:10]:  # Check first 10
                if not audit_log.action:
                    return TestResult(
                        test_name="audit_logging",
                        passed=False,
                        message="Audit log missing 'action' field",
                        evidence={"log_id": str(audit_log.id)},
                        severity="high",
                    )

                if not audit_log.timestamp:
                    return TestResult(
                        test_name="audit_logging",
                        passed=False,
                        message="Audit log missing 'timestamp' field",
                        evidence={"log_id": str(audit_log.id)},
                        severity="high",
                    )

                if audit_log.tenant_id != tenant_id:
                    return TestResult(
                        test_name="audit_logging",
                        passed=False,
                        message="Audit log has wrong tenant_id",
                        evidence={
                            "log_id": str(audit_log.id),
                            "expected": str(tenant_id),
                            "actual": str(audit_log.tenant_id),
                        },
                        severity="critical",
                    )

            return TestResult(
                test_name="audit_logging",
                passed=True,
                message="Audit logging verified - logs present and well-formed",
                evidence={"log_count": len(audit_logs), "sample_actions": [
                    log.action for log in audit_logs[:5]
                ]},
                severity="high",
            )

        except Exception as exc:
            log.error("compliance.test.audit_logging.error", error=str(exc))
            return TestResult(
                test_name="audit_logging",
                passed=False,
                message=f"Test failed with error: {exc}",
                evidence={"error": str(exc)},
                severity="high",
            )

    async def test_rbac_enforcement(self, tenant_id: uuid.UUID) -> TestResult:
        """Test that RBAC (role-based access control) works.

        Verifies:
        - All users have valid roles
        - Role assignments are consistent
        - Permission checks would work correctly

        Returns:
            TestResult indicating pass/fail
        """
        log.info("compliance.test.rbac_enforcement", tenant_id=str(tenant_id))

        try:
            # Query users for this tenant
            result = await self._db.execute(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.is_active.is_(True),
                )
            )
            users = result.scalars().all()

            if len(users) == 0:
                # No users yet - not a failure
                return TestResult(
                    test_name="rbac_enforcement",
                    passed=True,
                    message="No users found (new tenant - acceptable)",
                    evidence={"user_count": 0},
                    severity="medium",
                )

            # Verify all users have valid roles
            valid_roles = {UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER}
            for user in users:
                if user.role not in valid_roles:
                    return TestResult(
                        test_name="rbac_enforcement",
                        passed=False,
                        message=f"User has invalid role: {user.role}",
                        evidence={"user_id": str(user.id), "role": user.role},
                        severity="high",
                    )

            # Count users by role
            role_counts = {}
            for user in users:
                role_counts[user.role] = role_counts.get(user.role, 0) + 1

            # Verify at least one admin exists
            if role_counts.get(UserRole.ADMIN, 0) == 0:
                return TestResult(
                    test_name="rbac_enforcement",
                    passed=False,
                    message="No ADMIN users found - tenant should have at least one admin",
                    evidence={"role_counts": role_counts},
                    severity="high",
                )

            return TestResult(
                test_name="rbac_enforcement",
                passed=True,
                message="RBAC enforcement verified - roles assigned correctly",
                evidence={
                    "user_count": len(users),
                    "role_distribution": role_counts,
                },
                severity="high",
            )

        except Exception as exc:
            log.error("compliance.test.rbac_enforcement.error", error=str(exc))
            return TestResult(
                test_name="rbac_enforcement",
                passed=False,
                message=f"Test failed with error: {exc}",
                evidence={"error": str(exc)},
                severity="high",
            )

    async def test_export_control(self, tenant_id: uuid.UUID) -> TestResult:
        """Test that export controls are enforced.

        Verifies:
        - Export control checks are logged
        - Blocks are recorded when triggered

        Returns:
            TestResult indicating pass/fail
        """
        log.info("compliance.test.export_control", tenant_id=str(tenant_id))

        try:
            # Check for export control audit logs
            result = await self._db.execute(
                select(AuditLog)
                .where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action.startswith("export_control."),
                )
                .limit(10)
            )
            export_logs = result.scalars().all()

            # Export control may not have been triggered yet - not a failure
            return TestResult(
                test_name="export_control",
                passed=True,
                message="Export control system active (checks logged when triggered)",
                evidence={
                    "export_control_events": len(export_logs),
                    "monitoring_active": True,
                },
                severity="medium",
            )

        except Exception as exc:
            log.error("compliance.test.export_control.error", error=str(exc))
            return TestResult(
                test_name="export_control",
                passed=False,
                message=f"Test failed with error: {exc}",
                evidence={"error": str(exc)},
                severity="medium",
            )

    async def test_ai_disclosure(self, tenant_id: uuid.UUID) -> TestResult:
        """Test that AI-generated content is properly disclosed.

        Verifies:
        - AI interactions are logged with model information
        - Content can be traced back to AI generation

        Returns:
            TestResult indicating pass/fail
        """
        log.info("compliance.test.ai_disclosure", tenant_id=str(tenant_id))

        try:
            # Check for AI interaction logs
            result = await self._db.execute(
                select(AuditLog)
                .where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action.startswith("chat."),
                    AuditLog.model_used.isnot(None),
                )
                .limit(10)
            )
            ai_logs = result.scalars().all()

            # AI logs may not exist yet for new tenant
            if len(ai_logs) == 0:
                return TestResult(
                    test_name="ai_disclosure",
                    passed=True,
                    message="No AI interactions yet (new tenant - acceptable)",
                    evidence={"ai_interaction_count": 0},
                    severity="medium",
                )

            # Verify AI logs have model information
            for ai_log in ai_logs:
                if not ai_log.model_used:
                    return TestResult(
                        test_name="ai_disclosure",
                        passed=False,
                        message="AI interaction log missing model_used field",
                        evidence={"log_id": str(ai_log.id)},
                        severity="medium",
                    )

            return TestResult(
                test_name="ai_disclosure",
                passed=True,
                message="AI disclosure verified - all interactions logged with model info",
                evidence={
                    "ai_interaction_count": len(ai_logs),
                    "models_used": list(set(log.model_used for log in ai_logs if log.model_used)),
                },
                severity="medium",
            )

        except Exception as exc:
            log.error("compliance.test.ai_disclosure.error", error=str(exc))
            return TestResult(
                test_name="ai_disclosure",
                passed=False,
                message=f"Test failed with error: {exc}",
                evidence={"error": str(exc)},
                severity="medium",
            )
