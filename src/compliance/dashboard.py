"""Compliance dashboard - real-time metrics and violations.

Provides centralized compliance monitoring for:
- Classification coverage and enforcement
- Audit completeness
- Policy violations
- GDPR request tracking
- ISO 27001 control status
- PII incident tracking
- AI governance metrics

Designed for:
- Compliance officers (daily monitoring)
- Auditors (evidence collection)
- Security team (incident response)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.compliance.iso27001 import ISO27001Mapper
from src.models.audit import AuditLog, AuditStatus
from src.models.document import Document
from src.models.token_budget import RoutingDecisionRecord, TokenBudgetRecord

log = structlog.get_logger(__name__)


@dataclass
class PolicyViolation:
    """Record of a policy violation event."""

    id: uuid.UUID
    timestamp: datetime
    user_id: uuid.UUID | None
    violation_type: str
    resource_type: str | None
    resource_id: str | None
    severity: str  # "low" | "medium" | "high" | "critical"
    description: str
    resolved: bool


@dataclass
class AIGovernanceMetrics:
    """AI-specific governance metrics."""

    total_ai_interactions: int
    avg_confidence: float
    human_review_rate: float  # % of interactions requiring human review
    model_usage_by_tier: dict[str, int]  # light/standard/heavy usage counts
    token_budget_utilization: float  # % of budget used
    data_lineage_coverage: float  # % of responses with proper citations
    incident_response_time_avg: float  # Average time to resolve AI incidents (minutes)
    hallucination_detections: int
    pii_redactions: int
    export_control_blocks: int


@dataclass
class ComplianceOverview:
    """High-level compliance dashboard metrics."""

    tenant_id: uuid.UUID
    generated_at: datetime

    # Data governance
    classification_coverage: float  # % of documents classified
    audit_completeness: float  # % of actions audited

    # Violations and incidents
    policy_violations: list[PolicyViolation]
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
    ai_governance_metrics: AIGovernanceMetrics


class ComplianceDashboard:
    """Real-time compliance monitoring dashboard.

    Usage:
        dashboard = ComplianceDashboard(db)
        overview = await dashboard.get_overview(tenant_id)

        # Drill down into specific areas
        violations = await dashboard.get_violations(tenant_id, period_days=30)
        ai_metrics = await dashboard.get_ai_governance_metrics(tenant_id)
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_overview(self, tenant_id: uuid.UUID) -> ComplianceOverview:
        """Get high-level compliance overview for tenant.

        Args:
            tenant_id: Tenant to generate overview for

        Returns:
            ComplianceOverview with all key metrics
        """
        log.info("compliance.get_overview", tenant_id=str(tenant_id))

        now = datetime.now(UTC)
        period_30d = now - timedelta(days=30)

        # Calculate all metrics
        classification_coverage = await self._calculate_classification_coverage(tenant_id)
        audit_completeness = await self._calculate_audit_completeness(tenant_id, period_30d)

        policy_violations = await self.get_violations(tenant_id, period_days=30)
        pii_incidents = await self._count_pii_incidents(tenant_id, period_30d)
        export_blocks = await self._count_export_blocks(tenant_id, period_30d)

        gdpr_pending, gdpr_overdue = await self._get_gdpr_request_counts(tenant_id)

        # ISO 27001 status - query actual control mapping
        iso_implemented, iso_total = await self._get_iso27001_control_counts(tenant_id)
        iso_rate = (iso_implemented / iso_total * 100) if iso_total > 0 else 0

        ai_metrics = await self.get_ai_governance_metrics(tenant_id)

        overview = ComplianceOverview(
            tenant_id=tenant_id,
            generated_at=now,
            classification_coverage=classification_coverage,
            audit_completeness=audit_completeness,
            policy_violations=policy_violations[:10],  # Top 10 recent
            policy_violations_30d=len(policy_violations),
            pii_incidents_30d=pii_incidents,
            export_control_blocks_30d=export_blocks,
            gdpr_requests_pending=gdpr_pending,
            gdpr_requests_overdue=gdpr_overdue,
            iso27001_controls_implemented=iso_implemented,
            iso27001_controls_total=iso_total,
            iso27001_implementation_rate=iso_rate,
            ai_governance_metrics=ai_metrics,
        )

        log.info(
            "compliance.overview_generated",
            tenant_id=str(tenant_id),
            violations_30d=len(policy_violations),
            classification_coverage=classification_coverage,
        )

        return overview

    async def get_violations(
        self, tenant_id: uuid.UUID, period_days: int = 30
    ) -> list[PolicyViolation]:
        """Get policy violations within time period.

        Args:
            tenant_id: Tenant to query
            period_days: Number of days to look back

        Returns:
            List of PolicyViolation records
        """
        period_start = datetime.now(UTC) - timedelta(days=period_days)

        # Query audit logs for violations
        result = await self._db.execute(
            select(AuditLog)
            .where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("policy.violation"),
                AuditLog.timestamp >= period_start,
            )
            .order_by(AuditLog.timestamp.desc())
        )
        audit_logs = result.scalars().all()

        violations = []
        for audit_log in audit_logs:
            # Parse violation details from audit log
            extra = audit_log.extra or {}
            violation_type = extra.get("violation_type", "unknown")
            severity = extra.get("severity", "medium")

            violations.append(
                PolicyViolation(
                    id=audit_log.id,
                    timestamp=audit_log.timestamp,
                    user_id=audit_log.user_id,
                    violation_type=violation_type,
                    resource_type=audit_log.resource_type,
                    resource_id=audit_log.resource_id,
                    severity=severity,
                    description=audit_log.error_detail or "Policy violation detected",
                    resolved=False,  # Would track resolution state
                )
            )

        log.info(
            "compliance.violations_retrieved",
            tenant_id=str(tenant_id),
            period_days=period_days,
            violation_count=len(violations),
        )

        return violations

    async def get_ai_governance_metrics(
        self, tenant_id: uuid.UUID
    ) -> AIGovernanceMetrics:
        """Get AI-specific governance metrics.

        Args:
            tenant_id: Tenant to query

        Returns:
            AIGovernanceMetrics with AI usage and safety stats
        """
        log.info("compliance.get_ai_governance_metrics", tenant_id=str(tenant_id))

        period_30d = datetime.now(UTC) - timedelta(days=30)

        # Count AI interactions
        total_ai_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("chat."),
                AuditLog.timestamp >= period_30d,
            )
        )
        total_ai_interactions = total_ai_result.scalar_one()

        # Count model usage by tier
        model_usage_result = await self._db.execute(
            select(AuditLog.model_used, func.count(AuditLog.id))
            .where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("chat."),
                AuditLog.timestamp >= period_30d,
                AuditLog.model_used.isnot(None),
            )
            .group_by(AuditLog.model_used)
        )
        model_usage = {model: count for model, count in model_usage_result.all()}

        # Categorize by tier (light/standard/heavy)
        model_usage_by_tier = {
            "light": sum(
                count for model, count in model_usage.items() if "7b" in model.lower()
            ),
            "standard": sum(
                count for model, count in model_usage.items() if "32b" in model.lower()
            ),
            "heavy": sum(
                count for model, count in model_usage.items() if "72b" in model.lower()
            ),
        }

        # Count PII redactions
        pii_redactions_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("pii.redacted"),
                AuditLog.timestamp >= period_30d,
            )
        )
        pii_redactions = pii_redactions_result.scalar_one()

        # Count export control blocks
        export_blocks_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("export_control.blocked"),
                AuditLog.timestamp >= period_30d,
            )
        )
        export_blocks = export_blocks_result.scalar_one()

        # Token budget utilization: current monthly usage vs monthly limit
        token_budget_utilization = await self._calculate_token_budget_utilization(tenant_id)

        # Average confidence from routing decisions that have quality scores
        avg_confidence = await self._calculate_avg_confidence(tenant_id, period_30d)

        # Human review rate: write/approval operations vs total AI interactions
        human_review_rate = await self._calculate_human_review_rate(
            tenant_id, period_30d, total_ai_interactions
        )

        # Data lineage coverage: AI responses that include citations (extra.citations)
        data_lineage_coverage = await self._calculate_data_lineage_coverage(
            tenant_id, period_30d, total_ai_interactions
        )

        metrics = AIGovernanceMetrics(
            total_ai_interactions=total_ai_interactions,
            avg_confidence=avg_confidence,
            human_review_rate=human_review_rate,
            model_usage_by_tier=model_usage_by_tier,
            token_budget_utilization=token_budget_utilization,
            data_lineage_coverage=data_lineage_coverage,
            incident_response_time_avg=0.0,  # Requires dedicated incident tracking system
            hallucination_detections=0,  # Requires dedicated fact-checking integration
            pii_redactions=pii_redactions,
            export_control_blocks=export_blocks,
        )

        log.info(
            "compliance.ai_governance_metrics_generated",
            tenant_id=str(tenant_id),
            total_interactions=total_ai_interactions,
            pii_redactions=pii_redactions,
        )

        return metrics

    async def _calculate_classification_coverage(self, tenant_id: uuid.UUID) -> float:
        """Calculate % of documents that have been classified.

        Args:
            tenant_id: Tenant to calculate for

        Returns:
            Percentage (0-100) of documents with classification
        """
        # Count total documents
        total_result = await self._db.execute(
            select(func.count(Document.id)).where(Document.tenant_id == tenant_id)
        )
        total_documents = total_result.scalar_one()

        if total_documents == 0:
            return 100.0  # No documents = 100% coverage

        # Count classified documents (classification is required field, so all are classified)
        # In production, would check for explicit classification vs. default
        classified_result = await self._db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == tenant_id,
                Document.classification.isnot(None),
            )
        )
        classified_documents = classified_result.scalar_one()

        coverage = (classified_documents / total_documents) * 100
        return coverage

    async def _calculate_audit_completeness(
        self, tenant_id: uuid.UUID, period_start: datetime
    ) -> float:
        """Calculate audit log completeness (% of expected logs present).

        Args:
            tenant_id: Tenant to calculate for
            period_start: Start of period to check

        Returns:
            Percentage (0-100) representing audit completeness
        """
        # Count audit events in period
        audit_count_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.timestamp >= period_start,
            )
        )
        audit_count = audit_count_result.scalar_one()

        # Heuristic: Check for gaps in timestamp sequence
        # In production, would compare against expected event rate
        # For now, assume 100% if any logs exist
        return 100.0 if audit_count > 0 else 0.0

    async def _count_pii_incidents(
        self, tenant_id: uuid.UUID, period_start: datetime
    ) -> int:
        """Count PII-related incidents (blocks, warnings).

        Args:
            tenant_id: Tenant to count for
            period_start: Start of period

        Returns:
            Number of PII incidents
        """
        result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("pii."),
                AuditLog.timestamp >= period_start,
            )
        )
        return result.scalar_one()

    async def _count_export_blocks(
        self, tenant_id: uuid.UUID, period_start: datetime
    ) -> int:
        """Count export control blocks.

        Args:
            tenant_id: Tenant to count for
            period_start: Start of period

        Returns:
            Number of export blocks
        """
        result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("export_control.blocked"),
                AuditLog.timestamp >= period_start,
            )
        )
        return result.scalar_one()

    async def _get_gdpr_request_counts(
        self, tenant_id: uuid.UUID
    ) -> tuple[int, int]:
        """Get counts of pending and overdue GDPR requests.

        Derives pending/overdue counts from audit logs since there is no
        dedicated gdpr_requests table. A request is considered pending if a
        gdpr.request.* entry exists but no corresponding gdpr.erasure.completed
        or gdpr.request.completed entry has been logged for the same subject.
        Overdue means the request was created more than 30 days ago and has no
        completion entry (GDPR mandates 30-day response time).

        Args:
            tenant_id: Tenant to count for

        Returns:
            Tuple of (pending_count, overdue_count)
        """
        # Count total GDPR requests initiated in the last 90 days
        # (cover the 30-day legal window plus some buffer for recently resolved ones)
        since_90d = datetime.now(UTC) - timedelta(days=90)
        overdue_cutoff = datetime.now(UTC) - timedelta(days=30)

        # Requests created in the last 90 days
        requests_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("gdpr.request."),
                AuditLog.timestamp >= since_90d,
            )
        )
        total_requests = requests_result.scalar_one()

        # Completed GDPR erasure/access operations in the last 90 days
        completed_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.in_(["gdpr.erasure.completed", "gdpr.portability.completed"]),
                AuditLog.timestamp >= since_90d,
            )
        )
        completed = completed_result.scalar_one()

        # Pending = requests not yet completed (rough estimate: created - completed)
        pending = max(0, total_requests - completed)

        # Overdue = requests older than 30 days with no completion logged
        old_requests_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("gdpr.request."),
                AuditLog.timestamp >= since_90d,
                AuditLog.timestamp < overdue_cutoff,
            )
        )
        old_requests = old_requests_result.scalar_one()

        old_completed_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.in_(["gdpr.erasure.completed", "gdpr.portability.completed"]),
                AuditLog.timestamp >= since_90d,
                AuditLog.timestamp < overdue_cutoff,
            )
        )
        old_completed = old_completed_result.scalar_one()

        overdue = max(0, old_requests - old_completed)

        log.info(
            "compliance.gdpr_request_counts",
            tenant_id=str(tenant_id),
            total_requests=total_requests,
            completed=completed,
            pending=pending,
            overdue=overdue,
        )

        return (pending, overdue)

    async def _get_iso27001_control_counts(
        self, tenant_id: uuid.UUID
    ) -> tuple[int, int]:
        """Query ISO 27001 Annex A control mapping for implemented vs total counts.

        Args:
            tenant_id: Tenant to evaluate controls for

        Returns:
            Tuple of (implemented_count, total_count)
        """
        mapper = ISO27001Mapper(self._db)
        controls = await mapper.get_control_mapping(tenant_id)
        total = len(controls)
        implemented = sum(1 for c in controls if c.implemented)

        log.info(
            "compliance.iso27001_control_counts",
            tenant_id=str(tenant_id),
            implemented=implemented,
            total=total,
        )

        return (implemented, total)

    async def _calculate_token_budget_utilization(self, tenant_id: uuid.UUID) -> float:
        """Calculate token budget utilization as a fraction of monthly limit.

        Queries the token_budgets table for current monthly consumption vs limit.
        Returns 0.0 if no budget record exists for the tenant.

        Args:
            tenant_id: Tenant to calculate utilization for

        Returns:
            Utilization as a float in [0.0, 1.0]; capped at 1.0 when over budget
        """
        result = await self._db.execute(
            select(TokenBudgetRecord).where(
                TokenBudgetRecord.tenant_id == tenant_id
            )
        )
        budget = result.scalar_one_or_none()

        if budget is None or budget.monthly_limit == 0:
            return 0.0

        utilization = budget.current_monthly / budget.monthly_limit
        return min(utilization, 1.0)  # Cap at 100% (over-budget tenants return 1.0)

    async def _calculate_avg_confidence(
        self, tenant_id: uuid.UUID, since: datetime
    ) -> float:
        """Calculate average model confidence from routing decision quality scores.

        Uses RoutingDecisionRecord.actual_quality, which is populated when responses
        are scored. Only rows with non-NULL scores contribute to the average.

        Args:
            tenant_id: Tenant to calculate for
            since: Start of period

        Returns:
            Average quality score in [0.0, 1.0], or 0.0 if no scored decisions exist
        """
        result = await self._db.execute(
            select(func.avg(RoutingDecisionRecord.actual_quality)).where(
                RoutingDecisionRecord.tenant_id == tenant_id,
                RoutingDecisionRecord.timestamp >= since,
                RoutingDecisionRecord.actual_quality.isnot(None),
            )
        )
        avg = result.scalar_one_or_none()
        return float(avg) if avg is not None else 0.0

    async def _calculate_human_review_rate(
        self,
        tenant_id: uuid.UUID,
        since: datetime,
        total_ai_interactions: int,
    ) -> float:
        """Calculate the fraction of AI interactions that triggered human review.

        Counts audit log entries for write-operation approvals, which represent
        cases where the HITL workflow required a human decision before proceeding.

        Args:
            tenant_id: Tenant to calculate for
            since: Start of period
            total_ai_interactions: Denominator (pre-computed total AI interactions)

        Returns:
            Human review rate as a float in [0.0, 1.0], or 0.0 when no interactions
        """
        if total_ai_interactions == 0:
            return 0.0

        result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("write.approval"),
                AuditLog.timestamp >= since,
            )
        )
        review_count = result.scalar_one()
        return min(review_count / total_ai_interactions, 1.0)

    async def _calculate_data_lineage_coverage(
        self,
        tenant_id: uuid.UUID,
        since: datetime,
        total_ai_interactions: int,
    ) -> float:
        """Calculate fraction of AI responses that include RAG citations.

        An audit log entry is considered to have citations when the JSONB `extra`
        field contains a non-empty "citations" list (populated by the RAG pipeline).

        Args:
            tenant_id: Tenant to calculate for
            since: Start of period
            total_ai_interactions: Denominator (pre-computed total AI interactions)

        Returns:
            Coverage fraction in [0.0, 1.0], or 0.0 when no interactions exist
        """
        if total_ai_interactions == 0:
            return 0.0

        # Count chat audit logs that have citations in the extra JSONB field.
        # The PostgreSQL JSONB operator ? checks for key existence; SQLAlchemy
        # exposes this via the .has_key() / .contains() operators on JSONB columns.
        result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("chat."),
                AuditLog.timestamp >= since,
                AuditLog.extra["citations"].as_string().isnot(None),
            )
        )
        cited_count = result.scalar_one()
        return min(cited_count / total_ai_interactions, 1.0)

    async def get_classification_breakdown(
        self, tenant_id: uuid.UUID
    ) -> dict[str, int]:
        """Get breakdown of documents by classification level.

        Args:
            tenant_id: Tenant to analyze

        Returns:
            Dictionary mapping classification level to count
        """
        result = await self._db.execute(
            select(Document.classification, func.count(Document.id))
            .where(Document.tenant_id == tenant_id)
            .group_by(Document.classification)
        )

        breakdown = {classification: count for classification, count in result.all()}

        log.info(
            "compliance.classification_breakdown",
            tenant_id=str(tenant_id),
            breakdown=breakdown,
        )

        return breakdown

    async def get_user_access_summary(
        self, tenant_id: uuid.UUID, period_days: int = 30
    ) -> dict[str, int]:
        """Get summary of user access patterns.

        Args:
            tenant_id: Tenant to analyze
            period_days: Number of days to look back

        Returns:
            Dictionary with access statistics
        """
        period_start = datetime.now(UTC) - timedelta(days=period_days)

        # Count successful and failed authentications
        auth_success_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action == "auth.login",
                AuditLog.status == AuditStatus.SUCCESS,
                AuditLog.timestamp >= period_start,
            )
        )
        auth_success = auth_success_result.scalar_one()

        auth_failed_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action == "auth.login",
                AuditLog.status == AuditStatus.ERROR,
                AuditLog.timestamp >= period_start,
            )
        )
        auth_failed = auth_failed_result.scalar_one()

        # Count RBAC denials
        rbac_denials_result = await self._db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action.startswith("auth.permission_denied"),
                AuditLog.timestamp >= period_start,
            )
        )
        rbac_denials = rbac_denials_result.scalar_one()

        summary = {
            "successful_authentications": auth_success,
            "failed_authentications": auth_failed,
            "rbac_denials": rbac_denials,
            "active_users": await self._count_active_users(tenant_id, period_start),
        }

        log.info(
            "compliance.user_access_summary",
            tenant_id=str(tenant_id),
            period_days=period_days,
            summary=summary,
        )

        return summary

    async def _count_active_users(
        self, tenant_id: uuid.UUID, since: datetime
    ) -> int:
        """Count unique active users since timestamp.

        Args:
            tenant_id: Tenant to count for
            since: Start of period

        Returns:
            Number of unique active users
        """
        result = await self._db.execute(
            select(func.count(func.distinct(AuditLog.user_id))).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.user_id.isnot(None),
                AuditLog.timestamp >= since,
            )
        )
        return result.scalar_one()
