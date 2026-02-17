"""Tests for approval module - tool approval workflow for sensitive operations.

Tests cover:
- Risk scoring for different operations
- High-risk operations require approval
- Low-risk operations auto-approve
- Approval workflow state machine
- Timeout escalation
- Pending queue management
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from src.connectors.approval import (
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    ToolApprovalWorkflow,
)


@pytest.fixture
def workflow():
    """ToolApprovalWorkflow instance."""
    return ToolApprovalWorkflow(
        default_timeout_seconds=5.0,  # Short timeout for testing
        auto_approve_low_risk=True,
    )


@pytest.fixture
def tenant_id():
    """Test tenant ID."""
    return uuid.uuid4()


@pytest.fixture
def user_id():
    """Test user ID."""
    return uuid.uuid4()


@pytest.fixture
def operator_id():
    """Test operator ID."""
    return uuid.uuid4()


class TestRiskLevelClassification:
    """Test risk level assignment for operations."""

    @pytest.mark.asyncio
    async def test_low_risk_auto_approved(self, workflow, tenant_id, user_id):
        """Test that low-risk operations are auto-approved."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="get_purchase_orders",
            params={"status": "open"},
            risk_level=RiskLevel.LOW,
            rationale="Read-only query",
        )

        assert request.status == ApprovalStatus.APPROVED
        assert request.reviewed_at is not None

    @pytest.mark.asyncio
    async def test_medium_risk_requires_approval(self, workflow, tenant_id, user_id):
        """Test that medium-risk operations require approval."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="update_inventory",
            params={"item_id": "12345", "quantity": 100},
            risk_level=RiskLevel.MEDIUM,
            rationale="Update inventory count",
        )

        assert request.status == ApprovalStatus.PENDING

    @pytest.mark.asyncio
    async def test_high_risk_requires_approval(self, workflow, tenant_id, user_id):
        """Test that high-risk operations require approval."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="create_purchase_order",
            params={"vendor_id": "V001", "amount": 50000},
            risk_level=RiskLevel.HIGH,
            rationale="Create PO for critical equipment",
        )

        assert request.status == ApprovalStatus.PENDING

    @pytest.mark.asyncio
    async def test_critical_risk_requires_approval(self, workflow, tenant_id, user_id):
        """Test that critical-risk operations require approval."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="delete_cost_center",
            params={"cost_center_id": "CC-001"},
            risk_level=RiskLevel.CRITICAL,
            rationale="Delete cost center",
        )

        assert request.status == ApprovalStatus.PENDING


class TestApprovalWorkflow:
    """Test approval workflow state machine."""

    @pytest.mark.asyncio
    async def test_approve_pending_request(self, workflow, tenant_id, user_id, operator_id):
        """Test approving a pending request."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="create_purchase_order",
            params={"vendor_id": "V001"},
            risk_level=RiskLevel.HIGH,
            rationale="Create PO",
        )

        assert request.status == ApprovalStatus.PENDING

        # Approve request
        approved = await workflow.approve(
            request_id=request.request_id,
            approved_by=operator_id,
            tenant_id=tenant_id,
        )

        assert approved is True

        # Check updated status
        updated_request = workflow.get_request(request.request_id)
        assert updated_request.status == ApprovalStatus.APPROVED
        assert updated_request.approved_by == operator_id

    @pytest.mark.asyncio
    async def test_deny_pending_request(self, workflow, tenant_id, user_id, operator_id):
        """Test denying a pending request."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="create_purchase_order",
            params={"vendor_id": "V001"},
            risk_level=RiskLevel.HIGH,
            rationale="Create PO",
        )

        # Deny request
        denied = await workflow.deny(
            request_id=request.request_id,
            denied_by=operator_id,
            reason="Insufficient budget approval",
            tenant_id=tenant_id,
        )

        assert denied is True

        # Check updated status
        updated_request = workflow.get_request(request.request_id)
        assert updated_request.status == ApprovalStatus.DENIED
        assert updated_request.denial_reason == "Insufficient budget approval"
        assert updated_request.approved_by == operator_id  # Tracks who reviewed

    @pytest.mark.asyncio
    async def test_cannot_approve_already_reviewed(self, workflow, tenant_id, user_id, operator_id):
        """Test that already-reviewed requests cannot be approved again."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="create_purchase_order",
            params={},
            risk_level=RiskLevel.HIGH,
            rationale="Test",
        )

        # Approve first time
        await workflow.approve(request_id=request.request_id, approved_by=operator_id, tenant_id=tenant_id)

        # Try to approve again
        approved_again = await workflow.approve(request_id=request.request_id, approved_by=operator_id, tenant_id=tenant_id)

        assert approved_again is False

    @pytest.mark.asyncio
    async def test_cannot_deny_already_reviewed(self, workflow, tenant_id, user_id, operator_id):
        """Test that already-reviewed requests cannot be denied again."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="create_purchase_order",
            params={},
            risk_level=RiskLevel.HIGH,
            rationale="Test",
        )

        # Approve first
        await workflow.approve(request_id=request.request_id, approved_by=operator_id, tenant_id=tenant_id)

        # Try to deny
        denied = await workflow.deny(
            request_id=request.request_id,
            denied_by=operator_id,
            reason="Test",
            tenant_id=tenant_id,
        )

        assert denied is False

    @pytest.mark.asyncio
    async def test_approve_nonexistent_request(self, workflow, operator_id, tenant_id):
        """Test approving a nonexistent request."""
        fake_request_id = str(uuid.uuid4())

        approved = await workflow.approve(
            request_id=fake_request_id,
            approved_by=operator_id,
            tenant_id=tenant_id,
        )

        assert approved is False

    @pytest.mark.asyncio
    async def test_deny_nonexistent_request(self, workflow, operator_id, tenant_id):
        """Test denying a nonexistent request."""
        fake_request_id = str(uuid.uuid4())

        denied = await workflow.deny(
            request_id=fake_request_id,
            denied_by=operator_id,
            reason="Test",
            tenant_id=tenant_id,
        )

        assert denied is False


class TestTimeoutEscalation:
    """Test timeout handling for approval requests."""

    @pytest.mark.asyncio
    async def test_timeout_expires_request(self, tenant_id, user_id):
        """Test that requests timeout after the specified duration."""
        workflow = ToolApprovalWorkflow(
            default_timeout_seconds=0.5,  # 500ms timeout
            auto_approve_low_risk=False,
        )

        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op",
            params={},
            risk_level=RiskLevel.MEDIUM,
            rationale="Test timeout",
        )

        # Wait for approval with timeout
        response = await workflow.wait_for_approval(
            request_id=request.request_id,
            poll_interval_seconds=0.1,
        )

        assert response.approved is False
        assert response.status == ApprovalStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_custom_timeout(self, tenant_id, user_id):
        """Test custom timeout per request."""
        workflow = ToolApprovalWorkflow(default_timeout_seconds=10.0)

        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op",
            params={},
            risk_level=RiskLevel.MEDIUM,
            rationale="Test",
            timeout_seconds=0.5,  # Override default
        )

        assert request.timeout_seconds == 0.5

    @pytest.mark.asyncio
    async def test_wait_for_approval_approved(self, workflow, tenant_id, user_id, operator_id):
        """Test waiting for approval that gets approved."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op",
            params={},
            risk_level=RiskLevel.MEDIUM,
            rationale="Test",
        )

        # Approve in background after short delay
        async def approve_after_delay():
            await asyncio.sleep(0.2)
            await workflow.approve(request_id=request.request_id, approved_by=operator_id, tenant_id=tenant_id)

        asyncio.create_task(approve_after_delay())

        # Wait for approval
        response = await workflow.wait_for_approval(
            request_id=request.request_id,
            poll_interval_seconds=0.1,
        )

        assert response.approved is True
        assert response.status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_wait_for_approval_denied(self, workflow, tenant_id, user_id, operator_id):
        """Test waiting for approval that gets denied."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op",
            params={},
            risk_level=RiskLevel.MEDIUM,
            rationale="Test",
        )

        # Deny in background
        async def deny_after_delay():
            await asyncio.sleep(0.2)
            await workflow.deny(
                request_id=request.request_id,
                denied_by=operator_id,
                reason="Test denial",
                tenant_id=tenant_id,
            )

        asyncio.create_task(deny_after_delay())

        # Wait for approval
        response = await workflow.wait_for_approval(
            request_id=request.request_id,
            poll_interval_seconds=0.1,
        )

        assert response.approved is False
        assert response.status == ApprovalStatus.DENIED
        assert response.denial_reason == "Test denial"

    @pytest.mark.asyncio
    async def test_wait_for_approval_already_approved(self, workflow, tenant_id, user_id):
        """Test waiting for an already-approved (auto-approved) request."""
        # Low-risk requests are auto-approved
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op",
            params={},
            risk_level=RiskLevel.LOW,
            rationale="Test",
        )

        # Should return immediately
        response = await workflow.wait_for_approval(request_id=request.request_id)

        assert response.approved is True
        assert response.status == ApprovalStatus.APPROVED


class TestPendingQueue:
    """Test pending request queue management."""

    @pytest.mark.asyncio
    async def test_get_pending_requests_for_tenant(self, workflow, tenant_id, user_id):
        """Test retrieving all pending requests for a tenant."""
        # Create multiple pending requests
        for i in range(3):
            await workflow.request_approval(
                tenant_id=tenant_id,
                user_id=user_id,
                tool_name="sap_connector",
                operation=f"test_op_{i}",
                params={},
                risk_level=RiskLevel.MEDIUM,
                rationale=f"Test {i}",
            )

        pending = workflow.get_pending_requests(tenant_id=tenant_id)

        assert len(pending) == 3
        # Should be sorted by created_at
        assert pending[0].created_at <= pending[1].created_at <= pending[2].created_at

    @pytest.mark.asyncio
    async def test_get_pending_requests_by_tenant(self, workflow, user_id):
        """Test retrieving pending requests filtered by tenant."""
        tenant_1 = uuid.uuid4()
        tenant_2 = uuid.uuid4()

        # Create requests for different tenants
        await workflow.request_approval(
            tenant_id=tenant_1,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op_1",
            params={},
            risk_level=RiskLevel.MEDIUM,
            rationale="Tenant 1",
        )

        await workflow.request_approval(
            tenant_id=tenant_2,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op_2",
            params={},
            risk_level=RiskLevel.MEDIUM,
            rationale="Tenant 2",
        )

        # Get pending for tenant 1 only
        pending_t1 = workflow.get_pending_requests(tenant_id=tenant_1)

        assert len(pending_t1) == 1
        assert pending_t1[0].tenant_id == tenant_1

    @pytest.mark.asyncio
    async def test_clear_completed_requests(self, workflow, tenant_id, user_id, operator_id):
        """Test clearing old completed requests."""
        # Create and approve a request
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op",
            params={},
            risk_level=RiskLevel.MEDIUM,
            rationale="Test",
        )

        await workflow.approve(request_id=request.request_id, approved_by=operator_id, tenant_id=tenant_id)

        # Clear completed (with 0 seconds threshold to clear immediately)
        cleared_count = workflow.clear_completed(older_than_seconds=0.0)

        assert cleared_count == 1

        # Verify request is gone
        retrieved_request = workflow.get_request(request.request_id)
        assert retrieved_request is None

    @pytest.mark.asyncio
    async def test_clear_completed_preserves_pending(self, workflow, tenant_id, user_id):
        """Test that clearing completed requests preserves pending ones."""
        # Create pending request
        pending_request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op",
            params={},
            risk_level=RiskLevel.MEDIUM,
            rationale="Test",
        )

        # Clear completed
        workflow.clear_completed(older_than_seconds=0.0)

        # Pending request should still exist
        retrieved_request = workflow.get_request(pending_request.request_id)
        assert retrieved_request is not None
        assert retrieved_request.status == ApprovalStatus.PENDING


class TestApprovalRequestSerialization:
    """Test ApprovalRequest serialization."""

    @pytest.mark.asyncio
    async def test_to_dict(self, workflow, tenant_id, user_id):
        """Test converting ApprovalRequest to dictionary."""
        request = await workflow.request_approval(
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="sap_connector",
            operation="test_op",
            params={"key": "value"},
            risk_level=RiskLevel.MEDIUM,
            rationale="Test rationale",
        )

        request_dict = request.to_dict()

        assert request_dict["request_id"] == request.request_id
        assert request_dict["tenant_id"] == str(tenant_id)
        assert request_dict["user_id"] == str(user_id)
        assert request_dict["tool_name"] == "sap_connector"
        assert request_dict["operation"] == "test_op"
        assert request_dict["params"] == {"key": "value"}
        assert request_dict["risk_level"] == "medium"
        assert request_dict["rationale"] == "Test rationale"
        assert request_dict["status"] == "pending"


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_wait_for_nonexistent_request(self, workflow):
        """Test waiting for a nonexistent request."""
        fake_request_id = str(uuid.uuid4())

        response = await workflow.wait_for_approval(request_id=fake_request_id)

        assert response.approved is False
        assert response.status == ApprovalStatus.DENIED
        assert "not found" in response.denial_reason.lower()

    def test_is_expired_check(self, workflow, tenant_id, user_id):
        """Test ApprovalRequest.is_expired() method."""
        import time

        request = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            tool_name="test",
            operation="test",
            params={},
            risk_level=RiskLevel.MEDIUM,
            rationale="Test",
            timeout_seconds=0.1,  # 100ms
        )

        # Should not be expired initially
        assert request.is_expired() is False

        # Wait for expiration
        time.sleep(0.2)

        # Should be expired now
        assert request.is_expired() is True
