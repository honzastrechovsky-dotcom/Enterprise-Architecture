"""Data classification enforcement.

Implements a 4-tier classification system:
- Class I (General): No restrictions within tenant
- Class II (Confidential): Default. Standard RBAC applies
- Class III (Critical): Need-to-know. Document-level ACL. Audit every access
- Class IV (Restricted): Requires data owner approval. Export control checks
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

import structlog

from src.models.user import UserRole

log = structlog.get_logger(__name__)


class DataClassification(StrEnum):
    """Four-tier data classification levels."""

    CLASS_I = "class_i"
    CLASS_II = "class_ii"
    CLASS_III = "class_iii"
    CLASS_IV = "class_iv"


@dataclass
class ClassificationCheckResult:
    """Result of a classification access check."""

    allowed: bool
    requires_approval: bool
    requires_audit: bool
    reason: str


class ClassificationPolicy:
    """Enforces data classification access rules.

    Usage:
        policy = ClassificationPolicy()
        result = policy.check_access(
            user_role=UserRole.OPERATOR,
            classification=DataClassification.CLASS_III,
            document_acl=[user_id],
            user_id=user_id,
        )
        if not result.allowed:
            raise HTTPException(403, detail=result.reason)
    """

    def check_access(
        self,
        user_role: UserRole,
        classification: DataClassification,
        *,
        document_acl: list[uuid.UUID] | None = None,
        user_id: uuid.UUID | None = None,
    ) -> ClassificationCheckResult:
        """Check whether user can access a document with the given classification.

        Class I: Always allowed for authenticated users
        Class II: Standard RBAC applies (all roles within tenant)
        Class III: Requires OPERATOR+ role AND user must be in document_acl
        Class IV: Always denied without pre-approval (requires_approval=True)

        Args:
            user_role: User's role within the tenant
            classification: Document's data classification level
            document_acl: List of user IDs with explicit access (for Class III)
            user_id: User's ID (required for Class III ACL check)

        Returns:
            ClassificationCheckResult with access decision and metadata
        """
        if classification == DataClassification.CLASS_I:
            # Class I: General. No restrictions within tenant.
            log.debug(
                "classification.access_check",
                classification=classification,
                allowed=True,
                reason="class_i_general",
            )
            return ClassificationCheckResult(
                allowed=True,
                requires_approval=False,
                requires_audit=False,
                reason="Class I data is accessible to all authenticated users within tenant",
            )

        if classification == DataClassification.CLASS_II:
            # Class II: Confidential. Default. Standard RBAC applies.
            log.debug(
                "classification.access_check",
                classification=classification,
                user_role=user_role,
                allowed=True,
                reason="class_ii_confidential",
            )
            return ClassificationCheckResult(
                allowed=True,
                requires_approval=False,
                requires_audit=False,
                reason="Class II data is accessible to all roles within tenant",
            )

        if classification == DataClassification.CLASS_III:
            # Class III: Critical. Need-to-know. Document-level ACL.
            # Requires OPERATOR+ role AND explicit ACL membership.
            if user_role == UserRole.VIEWER:
                log.warning(
                    "classification.access_denied",
                    classification=classification,
                    user_role=user_role,
                    reason="insufficient_role",
                )
                return ClassificationCheckResult(
                    allowed=False,
                    requires_approval=False,
                    requires_audit=True,
                    reason="Class III data requires OPERATOR or higher role",
                )

            if document_acl is None or user_id is None:
                log.warning(
                    "classification.access_denied",
                    classification=classification,
                    reason="missing_acl_or_user_id",
                )
                return ClassificationCheckResult(
                    allowed=False,
                    requires_approval=False,
                    requires_audit=True,
                    reason="Class III data requires document-level ACL check",
                )

            if user_id not in document_acl:
                log.warning(
                    "classification.access_denied",
                    classification=classification,
                    user_id=str(user_id),
                    reason="not_in_acl",
                )
                return ClassificationCheckResult(
                    allowed=False,
                    requires_approval=False,
                    requires_audit=True,
                    reason="User not authorized for this Class III document (need-to-know)",
                )

            log.info(
                "classification.access_granted",
                classification=classification,
                user_id=str(user_id),
                user_role=user_role,
            )
            return ClassificationCheckResult(
                allowed=True,
                requires_approval=False,
                requires_audit=True,  # Every Class III access must be audited
                reason="Class III access granted via ACL",
            )

        if classification == DataClassification.CLASS_IV:
            # Class IV: Restricted. Requires data owner approval.
            # Never auto-approve.
            log.warning(
                "classification.access_blocked",
                classification=classification,
                user_role=user_role,
                reason="requires_approval",
            )
            return ClassificationCheckResult(
                allowed=False,
                requires_approval=True,
                requires_audit=True,
                reason="Class IV data requires explicit data owner approval",
            )

        # Fallback: unknown classification
        log.error("classification.unknown", classification=classification)
        return ClassificationCheckResult(
            allowed=False,
            requires_approval=False,
            requires_audit=True,
            reason=f"Unknown classification: {classification}",
        )

    def can_include_in_prompt(self, classification: DataClassification) -> bool:
        """Check if data with this classification can be included in AI prompts.

        Class I, II: Yes
        Class III: Yes (if access was granted)
        Class IV: No (never include without explicit approval)

        This is checked AFTER check_access() has already granted permission.
        """
        if classification in (
            DataClassification.CLASS_I,
            DataClassification.CLASS_II,
            DataClassification.CLASS_III,
        ):
            return True

        if classification == DataClassification.CLASS_IV:
            log.warning(
                "classification.prompt_inclusion_blocked",
                classification=classification,
            )
            return False

        log.error("classification.unknown_for_prompt", classification=classification)
        return False

    def requires_audit(self, classification: DataClassification) -> bool:
        """Check if this classification level requires individual audit logs.

        Class I: False (standard access, no special audit)
        Class II: False (standard audit covers it)
        Class III: True (every access must be individually audited)
        Class IV: True (every access attempt must be audited)
        """
        return classification in (
            DataClassification.CLASS_III,
            DataClassification.CLASS_IV,
        )
