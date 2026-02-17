"""Escalation service for approval timeouts.

Handles approval request escalation when operators don't respond:
1. Check for timed-out approval requests
2. Escalate to next level in chain (e.g., OPERATOR → ADMIN)
3. Auto-deny if all escalation levels exhausted

Default policy:
- 30 minutes: Escalate from OPERATOR to ADMIN
- 60 minutes total: Auto-deny if still not reviewed

Background task should call check_timeouts() periodically.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from src.models.user import UserRole
from src.operations.notification import NotificationService
from src.operations.write_framework import OperationStatus, WriteOperation, WriteOperationExecutor

log = structlog.get_logger(__name__)


@dataclass
class EscalationPolicy:
    """Policy for approval request escalation.

    Defines timeout and escalation chain:
    - timeout_minutes: Time before escalation
    - escalation_chain: Ordered list of roles to escalate through
    """

    timeout_minutes: int
    escalation_chain: list[UserRole]


class EscalationService:
    """Service for handling approval request escalation.

    Monitors pending approval requests and escalates or auto-denies
    based on configured policy.
    """

    def __init__(
        self,
        executor: WriteOperationExecutor,
        policy: EscalationPolicy | None = None,
        notification_service: NotificationService | None = None,
    ) -> None:
        """Initialize escalation service.

        Args:
            executor: WriteOperationExecutor to monitor
            policy: EscalationPolicy (default: 30min OPERATOR→ADMIN, 60min auto-deny)
            notification_service: Optional NotificationService used to alert the next
                role in the escalation chain.  When None, escalation events are only
                logged (backward-compatible behaviour).
        """
        self.executor = executor
        self.policy = policy or EscalationPolicy(
            timeout_minutes=30,
            escalation_chain=[UserRole.OPERATOR, UserRole.ADMIN],
        )
        self.notification_service = notification_service
        # Track escalation state: operation_id -> escalation_level
        self._escalation_state: dict[str, int] = {}

    async def check_timeouts(self) -> list[str]:
        """Check for timed-out approval requests and escalate.

        Should be called periodically by background task (e.g., every 5 minutes).

        Returns:
            List of operation IDs that were escalated or auto-denied
        """
        current_time = time.time()
        escalated_ops: list[str] = []

        # Get all pending operations via the public API (cross-tenant).
        # For DB-backed deployments, use PersistentWriteOperationExecutor instead.
        all_ops = self.executor.get_pending_operations()

        for operation in all_ops:
            if operation.status != OperationStatus.PENDING_APPROVAL:
                continue

            # Calculate time pending
            proposed_timestamp = operation.proposed_at.timestamp()
            time_pending_minutes = (current_time - proposed_timestamp) / 60

            # Check if timeout exceeded
            if time_pending_minutes >= self.policy.timeout_minutes:
                # Get current escalation level
                escalation_level = self._escalation_state.get(operation.id, 0)

                # Check if we can escalate further
                if escalation_level < len(self.policy.escalation_chain) - 1:
                    # Escalate to next level
                    await self.escalate(operation.id)
                    escalated_ops.append(operation.id)
                else:
                    # All escalation levels exhausted - auto-deny
                    await self.auto_deny(operation.id)
                    escalated_ops.append(operation.id)

        if escalated_ops:
            log.info(
                "escalation.check_completed",
                escalated_count=len(escalated_ops),
            )

        return escalated_ops

    async def escalate(self, operation_id: str) -> bool:
        """Escalate an operation to the next approval level.

        Args:
            operation_id: Operation ID to escalate

        Returns:
            True if escalated successfully

        Raises:
            ValueError: If operation not found or not pending
        """
        try:
            operation = await self.executor.get_operation(operation_id)
        except ValueError:
            raise ValueError(f"Operation {operation_id} not found")

        if operation.status != OperationStatus.PENDING_APPROVAL:
            log.warning(
                "escalation.not_pending",
                operation_id=operation_id,
                status=operation.status,
            )
            return False

        # Get current escalation level
        current_level = self._escalation_state.get(operation_id, 0)
        next_level = current_level + 1

        # Check if we can escalate further
        if next_level >= len(self.policy.escalation_chain):
            log.warning(
                "escalation.max_level_reached",
                operation_id=operation_id,
                level=current_level,
            )
            return False

        # Update escalation state
        self._escalation_state[operation_id] = next_level
        next_role = self.policy.escalation_chain[next_level]

        # Add audit entry
        operation.add_audit_entry(
            "escalated",
            operation.user_id,
            from_level=current_level,
            to_level=next_level,
            to_role=next_role.value,
        )

        log.info(
            "escalation.escalated",
            operation_id=operation_id,
            from_level=current_level,
            to_level=next_level,
            to_role=next_role.value,
            tenant_id=str(operation.tenant_id),
        )

        # Notify the next role about the escalation via fire-and-forget so that
        # a notification failure never blocks the escalation itself.
        if self.notification_service is not None:
            asyncio.create_task(
                self._send_escalation_notification(operation, next_role.value),
                name=f"escalation_notify_{operation_id}",
            )
        else:
            log.debug(
                "escalation.notification_skipped",
                operation_id=operation_id,
                reason="no_notification_service",
            )

        return True

    async def auto_deny(self, operation_id: str) -> bool:
        """Auto-deny an operation after all escalation levels exhausted.

        Args:
            operation_id: Operation ID to auto-deny

        Returns:
            True if auto-denied successfully

        Raises:
            ValueError: If operation not found
        """
        try:
            operation = await self.executor.get_operation(operation_id)
        except ValueError:
            raise ValueError(f"Operation {operation_id} not found")

        if operation.status != OperationStatus.PENDING_APPROVAL:
            log.warning(
                "escalation.auto_deny_not_pending",
                operation_id=operation_id,
                status=operation.status,
            )
            return False

        # Reject the operation with system user
        operation.status = OperationStatus.REJECTED
        operation.add_audit_entry(
            "auto_denied",
            operation.user_id,
            reason="Approval timeout - all escalation levels exhausted",
        )

        # Clean up escalation state
        self._escalation_state.pop(operation_id, None)

        log.warning(
            "escalation.auto_denied",
            operation_id=operation_id,
            tenant_id=str(operation.tenant_id),
        )

        return True

    def get_escalation_level(self, operation_id: str) -> int:
        """Get current escalation level for an operation.

        Args:
            operation_id: Operation ID

        Returns:
            Escalation level (0 = initial, 1 = first escalation, etc.)
        """
        return self._escalation_state.get(operation_id, 0)

    def clear_escalation_state(self, operation_id: str) -> None:
        """Clear escalation state for an operation.

        Should be called when operation is approved/rejected.

        Args:
            operation_id: Operation ID
        """
        self._escalation_state.pop(operation_id, None)

    async def _send_escalation_notification(
        self,
        operation: WriteOperation,
        next_role: str,
    ) -> None:
        """Send an escalation notification via the notification service.

        This coroutine is scheduled with asyncio.create_task so failures are
        isolated from the escalation logic itself.  Any exception is caught and
        logged; the escalation is already recorded in the audit trail before
        this method is called.

        Args:
            operation: The write operation that was escalated.
            next_role: Human-readable name of the role now responsible.
        """
        if self.notification_service is None:
            return

        try:
            subject = f"Escalation: Approval Required — {operation.description}"
            body = (
                f"An approval request has been escalated to role '{next_role}'.\n\n"
                f"Operation ID : {operation.id}\n"
                f"Description  : {operation.description}\n"
                f"Connector    : {operation.connector}\n"
                f"Risk Level   : {operation.risk_level.upper()}\n"
                f"Proposed by  : {operation.user_id}\n"
                f"Proposed at  : {operation.proposed_at.isoformat()}\n\n"
                "Please review and approve or reject this operation."
            )

            # Best-effort: try email then webhook then fallback structured log.
            sent = False

            if self.notification_service.smtp_host:
                # Fire-and-forget is already handled one level up; we call the
                # internal _send_email directly since we are already inside a
                # background task.
                sent = await self.notification_service._send_email(
                    to=f"escalation+{next_role}@enterprise-agents.local",
                    subject=subject,
                    body=body,
                )

            if not sent and self.notification_service.webhook_url:
                sent = await self.notification_service._send_webhook(subject, body)

            if not sent:
                log.info(
                    "escalation.notification_fallback",
                    operation_id=operation.id,
                    next_role=next_role,
                    tenant_id=str(operation.tenant_id),
                    subject=subject,
                )

        except Exception as exc:
            log.error(
                "escalation.notification_failed",
                operation_id=operation.id,
                next_role=next_role,
                error=str(exc),
            )
