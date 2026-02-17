"""Tool approval workflow for sensitive operations.

ToolApprovalWorkflow provides operator review before executing high-risk tools:
- Write operations (data modification)
- Export operations (data exfiltration risk)
- High-cost operations (large API calls)

Workflow:
1. Agent requests approval for operation
2. Approval request queued with context
3. Operator reviews and approves/denies
4. Agent receives approval result
5. Auto-deny after timeout (default 5 minutes)

Current: In-memory queue (single process, non-persistent).
For multi-instance deployments, replace with a Redis-backed distributed queue.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class ApprovalStatus(StrEnum):
    """Status of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


class RiskLevel(StrEnum):
    """Risk level for tool operations."""

    LOW = "low"  # Auto-approve
    MEDIUM = "medium"  # Require approval
    HIGH = "high"  # Require approval + audit
    CRITICAL = "critical"  # Require approval + admin


@dataclass
class ApprovalRequest:
    """An approval request for a tool operation."""

    request_id: str
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    tool_name: str
    operation: str
    params: dict[str, Any]
    risk_level: RiskLevel
    rationale: str  # Why this operation is being requested
    created_at: float = field(default_factory=time.time)
    status: ApprovalStatus = ApprovalStatus.PENDING
    timeout_seconds: float = 300.0  # 5 minutes default
    approved_by: uuid.UUID | None = None
    reviewed_at: float | None = None
    denial_reason: str | None = None

    def is_expired(self) -> bool:
        """Check if approval request has timed out."""
        return time.time() > (self.created_at + self.timeout_seconds)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "request_id": self.request_id,
            "tenant_id": str(self.tenant_id),
            "user_id": str(self.user_id),
            "tool_name": self.tool_name,
            "operation": self.operation,
            "params": self.params,
            "risk_level": self.risk_level,
            "rationale": self.rationale,
            "status": self.status,
            "created_at": datetime.fromtimestamp(self.created_at, tz=UTC).isoformat(),
            "timeout_seconds": self.timeout_seconds,
            "approved_by": str(self.approved_by) if self.approved_by else None,
            "reviewed_at": datetime.fromtimestamp(self.reviewed_at, tz=UTC).isoformat()
            if self.reviewed_at
            else None,
            "denial_reason": self.denial_reason,
        }


@dataclass
class ApprovalResponse:
    """Response to an approval request."""

    request_id: str
    approved: bool
    status: ApprovalStatus
    approved_by: uuid.UUID | None = None
    denial_reason: str | None = None


class ToolApprovalWorkflow:
    """Approval workflow for sensitive tool operations.

    Maintains in-memory queue of approval requests.
    Operators retrieve pending requests and approve/deny.
    Agents poll for approval status.

    Thread-safe for async use (Python GIL protects dict operations).
    """

    def __init__(
        self,
        default_timeout_seconds: float = 300.0,
        auto_approve_low_risk: bool = True,
    ) -> None:
        """Initialize approval workflow.

        Args:
            default_timeout_seconds: Default timeout for approval requests
            auto_approve_low_risk: Auto-approve low-risk operations
        """
        self.default_timeout_seconds = default_timeout_seconds
        self.auto_approve_low_risk = auto_approve_low_risk
        # In-memory queue: request_id -> ApprovalRequest
        self._requests: dict[str, ApprovalRequest] = {}

    async def request_approval(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        tool_name: str,
        operation: str,
        params: dict[str, Any],
        risk_level: RiskLevel,
        rationale: str,
        timeout_seconds: float | None = None,
    ) -> ApprovalRequest:
        """Create an approval request and add to queue.

        Args:
            tenant_id: Tenant UUID
            user_id: User UUID requesting operation
            tool_name: Tool name (e.g., "sap_connector")
            operation: Operation name (e.g., "create_purchase_order")
            params: Operation parameters
            risk_level: Risk level for this operation
            rationale: Human-readable explanation of why
            timeout_seconds: Custom timeout (default: 5 minutes)

        Returns:
            ApprovalRequest with status PENDING or APPROVED (if auto-approved)
        """
        request_id = str(uuid.uuid4())
        timeout = timeout_seconds or self.default_timeout_seconds

        request = ApprovalRequest(
            request_id=request_id,
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name=tool_name,
            operation=operation,
            params=params,
            risk_level=risk_level,
            rationale=rationale,
            timeout_seconds=timeout,
        )

        # Auto-approve low-risk operations if enabled
        if self.auto_approve_low_risk and risk_level == RiskLevel.LOW:
            request.status = ApprovalStatus.APPROVED
            request.reviewed_at = time.time()
            self._requests[request_id] = request
            log.info(
                "approval.auto_approved",
                request_id=request_id,
                tool=tool_name,
                operation=operation,
                tenant_id=str(tenant_id),
            )
        else:
            # Add to pending queue
            self._requests[request_id] = request
            log.info(
                "approval.requested",
                request_id=request_id,
                tool=tool_name,
                operation=operation,
                risk_level=risk_level,
                tenant_id=str(tenant_id),
            )

        return request

    async def wait_for_approval(
        self,
        request_id: str,
        poll_interval_seconds: float = 2.0,
    ) -> ApprovalResponse:
        """Wait for approval request to be reviewed or timeout.

        Agent calls this after creating approval request.
        Polls until status changes from PENDING.

        Args:
            request_id: Request ID to wait for
            poll_interval_seconds: How often to check status

        Returns:
            ApprovalResponse with final status
        """
        request = self._requests.get(request_id)
        if not request:
            return ApprovalResponse(
                request_id=request_id,
                approved=False,
                status=ApprovalStatus.DENIED,
                denial_reason="Request not found",
            )

        # If already approved (auto-approve), return immediately
        if request.status == ApprovalStatus.APPROVED:
            return ApprovalResponse(
                request_id=request_id,
                approved=True,
                status=ApprovalStatus.APPROVED,
                approved_by=request.approved_by,
            )

        # Poll until status changes or timeout
        while request.status == ApprovalStatus.PENDING:
            # Check for timeout
            if request.is_expired():
                request.status = ApprovalStatus.TIMEOUT
                request.reviewed_at = time.time()
                log.warning(
                    "approval.timeout",
                    request_id=request_id,
                    tool=request.tool_name,
                    tenant_id=str(request.tenant_id),
                )
                return ApprovalResponse(
                    request_id=request_id,
                    approved=False,
                    status=ApprovalStatus.TIMEOUT,
                    denial_reason="Approval request timed out",
                )

            # Wait before polling again
            await asyncio.sleep(poll_interval_seconds)

        # Status changed - return result
        return ApprovalResponse(
            request_id=request_id,
            approved=(request.status == ApprovalStatus.APPROVED),
            status=request.status,
            approved_by=request.approved_by,
            denial_reason=request.denial_reason,
        )

    async def approve(
        self,
        request_id: str,
        approved_by: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> bool:
        """Approve a pending request.

        Operator calls this to approve an operation.

        Args:
            request_id: Request ID to approve
            approved_by: Operator user UUID
            tenant_id: Caller's tenant UUID - must match the request's tenant

        Returns:
            True if approved, False if request not found or already reviewed
        """
        request = self._requests.get(request_id)
        if not request:
            log.warning("approval.approve_failed", request_id=request_id, reason="not_found")
            return False

        # Cross-tenant isolation: caller must belong to same tenant as request
        if request.tenant_id != tenant_id:
            log.warning(
                "approval.cross_tenant_denied",
                request_id=request_id,
                request_tenant=str(request.tenant_id),
                caller_tenant=str(tenant_id),
            )
            return False

        if request.status != ApprovalStatus.PENDING:
            log.warning(
                "approval.approve_failed",
                request_id=request_id,
                reason="already_reviewed",
                current_status=request.status,
            )
            return False

        request.status = ApprovalStatus.APPROVED
        request.approved_by = approved_by
        request.reviewed_at = time.time()

        log.info(
            "approval.approved",
            request_id=request_id,
            tool=request.tool_name,
            operation=request.operation,
            approved_by=str(approved_by),
            tenant_id=str(request.tenant_id),
        )

        return True

    async def deny(
        self,
        request_id: str,
        denied_by: uuid.UUID,
        reason: str,
        tenant_id: uuid.UUID,
    ) -> bool:
        """Deny a pending request.

        Operator calls this to deny an operation.

        Args:
            request_id: Request ID to deny
            denied_by: Operator user UUID
            reason: Reason for denial
            tenant_id: Caller's tenant UUID - must match the request's tenant

        Returns:
            True if denied, False if request not found or already reviewed
        """
        request = self._requests.get(request_id)
        if not request:
            log.warning("approval.deny_failed", request_id=request_id, reason="not_found")
            return False

        # Cross-tenant isolation: caller must belong to same tenant as request
        if request.tenant_id != tenant_id:
            log.warning(
                "approval.cross_tenant_denied",
                request_id=request_id,
                request_tenant=str(request.tenant_id),
                caller_tenant=str(tenant_id),
            )
            return False

        if request.status != ApprovalStatus.PENDING:
            log.warning(
                "approval.deny_failed",
                request_id=request_id,
                reason="already_reviewed",
                current_status=request.status,
            )
            return False

        request.status = ApprovalStatus.DENIED
        request.approved_by = denied_by  # Track who reviewed it
        request.denial_reason = reason
        request.reviewed_at = time.time()

        log.info(
            "approval.denied",
            request_id=request_id,
            tool=request.tool_name,
            operation=request.operation,
            denied_by=str(denied_by),
            reason=reason,
            tenant_id=str(request.tenant_id),
        )

        return True

    def get_pending_requests(
        self,
        tenant_id: uuid.UUID,
    ) -> list[ApprovalRequest]:
        """Get all pending approval requests for a specific tenant.

        Operator dashboard calls this to show approval queue.
        tenant_id is mandatory to enforce tenant isolation.

        Args:
            tenant_id: Filter by tenant (mandatory)

        Returns:
            List of pending ApprovalRequests for the given tenant
        """
        pending = [
            req
            for req in self._requests.values()
            if req.status == ApprovalStatus.PENDING
            and req.tenant_id == tenant_id
        ]
        return sorted(pending, key=lambda r: r.created_at)

    def get_request(self, request_id: str) -> ApprovalRequest | None:
        """Get approval request by ID.

        Args:
            request_id: Request ID

        Returns:
            ApprovalRequest or None if not found
        """
        return self._requests.get(request_id)

    def clear_completed(self, older_than_seconds: float = 3600.0) -> int:
        """Clear completed/timed-out requests older than threshold.

        Background task should call this periodically to prevent memory growth.

        Args:
            older_than_seconds: Clear requests older than this (default 1 hour)

        Returns:
            Number of requests cleared
        """
        cutoff_time = time.time() - older_than_seconds
        to_remove = [
            req_id
            for req_id, req in self._requests.items()
            if req.status != ApprovalStatus.PENDING and req.created_at < cutoff_time
        ]

        for req_id in to_remove:
            del self._requests[req_id]

        if to_remove:
            log.info("approval.cleared_old_requests", count=len(to_remove))

        return len(to_remove)
