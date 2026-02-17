"""Tests for Write Operations Framework.

Tests demonstrate TDD approach for HITL write operations:
1. Write tests FIRST (Red phase)
2. Implement minimal code to pass (Green phase)
3. Refactor while keeping tests green

Following Test-First Imperative (Article III).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.connectors.base import ConnectorResult
from src.operations.write_framework import (
    ConnectorRegistry,
    OperationStatus,
    RiskLevel,
    WriteOperation,
    WriteOperationExecutor,
)


def _make_mock_registry(success: bool = True) -> ConnectorRegistry:
    """Return a ConnectorRegistry backed by a mock connector.

    The mock connector's ``execute()`` method returns a successful
    ``ConnectorResult`` by default, so unit tests that exercise the
    execution path don't need a live SAP/MES endpoint.
    """
    mock_connector = MagicMock()
    mock_connector.__aenter__ = AsyncMock(return_value=mock_connector)
    mock_connector.__aexit__ = AsyncMock(return_value=None)
    mock_connector.execute = AsyncMock(
        return_value=ConnectorResult(
            success=success,
            data={"mock": True},
            metadata={"test": "mock_registry"},
        )
    )

    registry = ConnectorRegistry()
    # Patch create() to always return the same mock regardless of connector name.
    registry.create = MagicMock(return_value=mock_connector)  # type: ignore[method-assign]
    return registry


class TestWriteOperation:
    """Tests for WriteOperation dataclass."""

    def test_create_write_operation(self) -> None:
        """Test creation of write operation with required fields."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        op = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="sap",
            operation_type="create_purchase_request",
            params={"material_id": "MAT-001", "quantity": 100},
            description="Create purchase request for 100 units of MAT-001",
            risk_level=RiskLevel.HIGH,
        )

        assert op.id is None  # Not persisted yet
        assert op.tenant_id == tenant_id
        assert op.user_id == user_id
        assert op.connector == "sap"
        assert op.operation_type == "create_purchase_request"
        assert op.params == {"material_id": "MAT-001", "quantity": 100}
        assert op.risk_level == RiskLevel.HIGH
        assert op.requires_approval is True  # Default for HIGH risk
        assert op.requires_mfa is False  # Default
        assert op.status == OperationStatus.PROPOSED
        assert op.audit_trail == []
        assert op.proposed_at is not None
        assert op.approved_at is None
        assert op.executed_at is None

    def test_critical_risk_requires_mfa(self) -> None:
        """Test that CRITICAL risk level sets requires_mfa=True."""
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="delete_production_order",
            params={},
            description="Delete production order",
            risk_level=RiskLevel.CRITICAL,
        )

        assert op.requires_approval is True
        assert op.requires_mfa is True  # CRITICAL requires MFA

    def test_low_risk_no_approval_required(self) -> None:
        """Test that LOW risk operations don't require approval."""
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="mes",
            operation_type="log_downtime_event",
            params={},
            description="Log downtime event",
            risk_level=RiskLevel.LOW,
        )

        assert op.requires_approval is False
        assert op.requires_mfa is False

    def test_audit_trail_structure(self) -> None:
        """Test audit trail is a list of dict entries."""
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="update_inventory",
            params={},
            description="Update inventory",
            risk_level=RiskLevel.MEDIUM,
        )

        # Add audit entry
        op.audit_trail.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "proposed",
            "actor": str(op.user_id),
        })

        assert len(op.audit_trail) == 1
        assert op.audit_trail[0]["event"] == "proposed"

    def test_to_dict_serialization(self) -> None:
        """Test operation can be serialized to dict."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        op = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="sap",
            operation_type="create_purchase_request",
            params={"material_id": "MAT-001"},
            description="Test operation",
            risk_level=RiskLevel.HIGH,
        )

        data = op.to_dict()

        assert data["tenant_id"] == str(tenant_id)
        assert data["user_id"] == str(user_id)
        assert data["connector"] == "sap"
        assert data["operation_type"] == "create_purchase_request"
        assert data["status"] == "proposed"
        assert data["risk_level"] == "high"
        assert "proposed_at" in data


class TestWriteOperationExecutor:
    """Tests for WriteOperationExecutor."""

    @pytest.fixture
    def executor(self) -> WriteOperationExecutor:
        """Fixture providing executor instance."""
        return WriteOperationExecutor()

    def test_executor_initialization(self, executor: WriteOperationExecutor) -> None:
        """Test executor initializes correctly."""
        assert executor is not None
        # WriteOperationExecutor uses in-memory storage (see PersistentWriteOperationExecutor for DB)
        assert hasattr(executor, "_operations")

    @pytest.mark.asyncio
    async def test_propose_operation(self, executor: WriteOperationExecutor) -> None:
        """Test proposing a write operation."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        op = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="sap",
            operation_type="create_purchase_request",
            params={"material_id": "MAT-001", "quantity": 100},
            description="Create PR for material MAT-001",
            risk_level=RiskLevel.HIGH,
        )

        # Propose the operation
        proposed = await executor.propose(op)

        # Should have ID after proposal; HIGH risk goes to PENDING_APPROVAL
        assert proposed.id is not None
        assert proposed.status == OperationStatus.PENDING_APPROVAL
        assert len(proposed.audit_trail) == 1
        assert proposed.audit_trail[0]["event"] == "proposed"

    @pytest.mark.asyncio
    async def test_propose_low_risk_auto_approves(
        self, executor: WriteOperationExecutor
    ) -> None:
        """Test that LOW risk operations are auto-approved."""
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="mes",
            operation_type="log_downtime_event",
            params={},
            description="Log downtime",
            risk_level=RiskLevel.LOW,
        )

        proposed = await executor.propose(op)

        # Should be auto-approved
        assert proposed.status == OperationStatus.APPROVED
        assert proposed.approved_at is not None

    @pytest.mark.asyncio
    async def test_approve_operation(self, executor: WriteOperationExecutor) -> None:
        """Test approving a pending operation."""
        # Propose operation first
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="update_inventory",
            params={},
            description="Update inventory",
            risk_level=RiskLevel.MEDIUM,
        )
        proposed = await executor.propose(op)
        assert proposed.status == OperationStatus.PENDING_APPROVAL

        # Approve it
        approver_id = uuid.uuid4()
        approved = await executor.approve(
            operation_id=proposed.id,
            approver_user_id=approver_id,
            mfa_verified=False,
        )

        assert approved.status == OperationStatus.APPROVED
        assert approved.approved_by == approver_id
        assert approved.approved_at is not None
        # Check audit trail
        approval_entry = next(
            (e for e in approved.audit_trail if e["event"] == "approved"), None
        )
        assert approval_entry is not None

    @pytest.mark.asyncio
    async def test_approve_requires_mfa_fails_without_mfa(
        self, executor: WriteOperationExecutor
    ) -> None:
        """Test that operations requiring MFA fail approval without MFA."""
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="delete_data",
            params={},
            description="Delete data",
            risk_level=RiskLevel.CRITICAL,
        )
        proposed = await executor.propose(op)
        assert proposed.requires_mfa is True

        # Try to approve without MFA
        with pytest.raises(ValueError, match="requires MFA verification"):
            await executor.approve(
                operation_id=proposed.id,
                approver_user_id=uuid.uuid4(),
                mfa_verified=False,
            )

    @pytest.mark.asyncio
    async def test_approve_with_mfa_succeeds(
        self, executor: WriteOperationExecutor
    ) -> None:
        """Test that operations requiring MFA succeed with MFA."""
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="delete_data",
            params={},
            description="Delete data",
            risk_level=RiskLevel.CRITICAL,
        )
        proposed = await executor.propose(op)

        # Approve with MFA
        approved = await executor.approve(
            operation_id=proposed.id,
            approver_user_id=uuid.uuid4(),
            mfa_verified=True,
        )

        assert approved.status == OperationStatus.APPROVED

    @pytest.mark.asyncio
    async def test_reject_operation(self, executor: WriteOperationExecutor) -> None:
        """Test rejecting a pending operation."""
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="create_purchase_request",
            params={},
            description="Create PR",
            risk_level=RiskLevel.HIGH,
        )
        proposed = await executor.propose(op)

        # Reject it
        rejector_id = uuid.uuid4()
        rejected = await executor.reject(
            operation_id=proposed.id,
            rejector_user_id=rejector_id,
            reason="Insufficient budget approval",
        )

        assert rejected.status == OperationStatus.REJECTED
        assert rejected.approved_by == rejector_id  # Track who rejected
        # Check audit trail
        reject_entry = next(
            (e for e in rejected.audit_trail if e["event"] == "rejected"), None
        )
        assert reject_entry is not None
        assert reject_entry["reason"] == "Insufficient budget approval"

    @pytest.mark.asyncio
    async def test_execute_approved_operation(self) -> None:
        """Test executing an approved operation via the connector registry.

        Uses a mock registry so the test does not require a live SAP/MES endpoint.
        """
        executor = WriteOperationExecutor(connector_registry=_make_mock_registry())
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="create_purchase_request",
            params={"material_id": "MAT-001"},
            description="Create PR",
            risk_level=RiskLevel.LOW,  # Auto-approves
        )
        proposed = await executor.propose(op)
        assert proposed.status == OperationStatus.APPROVED

        result = await executor.execute(operation_id=proposed.id)

        # ConnectorResult should be returned
        assert result is not None
        assert isinstance(result, ConnectorResult)
        # Operation should transition to COMPLETED when the connector succeeds
        executed_op = await executor.get_operation(proposed.id)
        assert executed_op.status == OperationStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_execute_without_registry_fails_gracefully(
        self, executor: WriteOperationExecutor
    ) -> None:
        """Execution without a connector registry returns a descriptive failure.

        The executor must NOT raise an exception — it must return a failed
        ``ConnectorResult`` and set the operation status to FAILED.
        """
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="create_purchase_request",
            params={"material_id": "MAT-001"},
            description="Create PR",
            risk_level=RiskLevel.LOW,
        )
        proposed = await executor.propose(op)
        assert proposed.status == OperationStatus.APPROVED

        result = await executor.execute(operation_id=proposed.id)

        assert not result.success
        assert "No connector registry configured" in (result.error or "")
        executed_op = await executor.get_operation(proposed.id)
        assert executed_op.status == OperationStatus.FAILED

    @pytest.mark.asyncio
    async def test_execute_non_approved_fails(
        self, executor: WriteOperationExecutor
    ) -> None:
        """Test that non-approved operations cannot be executed."""
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="create_purchase_request",
            params={},
            description="Create PR",
            risk_level=RiskLevel.HIGH,
        )
        proposed = await executor.propose(op)
        assert proposed.status == OperationStatus.PENDING_APPROVAL

        # Try to execute without approval
        with pytest.raises(ValueError, match="Only APPROVED operations can be executed"):
            await executor.execute(operation_id=proposed.id)

    @pytest.mark.asyncio
    async def test_get_pending_operations_by_tenant(
        self, executor: WriteOperationExecutor
    ) -> None:
        """Test retrieving pending operations for a tenant."""
        tenant_a = uuid.uuid4()
        tenant_b = uuid.uuid4()

        # Create operations for different tenants
        op1 = WriteOperation(
            tenant_id=tenant_a,
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="create_pr",
            params={},
            description="Tenant A op",
            risk_level=RiskLevel.HIGH,
        )
        op2 = WriteOperation(
            tenant_id=tenant_a,
            user_id=uuid.uuid4(),
            connector="mes",
            operation_type="update_status",
            params={},
            description="Tenant A op 2",
            risk_level=RiskLevel.MEDIUM,
        )
        op3 = WriteOperation(
            tenant_id=tenant_b,
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="create_pr",
            params={},
            description="Tenant B op",
            risk_level=RiskLevel.HIGH,
        )

        await executor.propose(op1)
        await executor.propose(op2)
        await executor.propose(op3)

        # Get pending for tenant A
        pending_a = await executor.get_pending(tenant_id=tenant_a)

        assert len(pending_a) == 2
        assert all(op.tenant_id == tenant_a for op in pending_a)
        assert all(
            op.status == OperationStatus.PENDING_APPROVAL for op in pending_a
        )

    @pytest.mark.asyncio
    async def test_get_history(self, executor: WriteOperationExecutor) -> None:
        """Test retrieving operation history."""
        tenant_id = uuid.uuid4()

        # Create and complete several operations
        for i in range(5):
            op = WriteOperation(
                tenant_id=tenant_id,
                user_id=uuid.uuid4(),
                connector="sap",
                operation_type=f"operation_{i}",
                params={},
                description=f"Operation {i}",
                risk_level=RiskLevel.LOW,  # Auto-approved
            )
            await executor.propose(op)

        # Get history
        history = await executor.get_history(tenant_id=tenant_id, limit=10)

        assert len(history) == 5
        # Should be ordered by proposed_at DESC (most recent first)
        assert history[0].operation_type == "operation_4"
        assert history[-1].operation_type == "operation_0"

    @pytest.mark.asyncio
    async def test_get_history_with_limit(
        self, executor: WriteOperationExecutor
    ) -> None:
        """Test history respects limit parameter."""
        tenant_id = uuid.uuid4()

        # Create 10 operations
        for i in range(10):
            op = WriteOperation(
                tenant_id=tenant_id,
                user_id=uuid.uuid4(),
                connector="sap",
                operation_type=f"op_{i}",
                params={},
                description=f"Op {i}",
                risk_level=RiskLevel.LOW,
            )
            await executor.propose(op)

        # Get only 3 most recent
        history = await executor.get_history(tenant_id=tenant_id, limit=3)

        assert len(history) == 3
        assert history[0].operation_type == "op_9"
        assert history[1].operation_type == "op_8"
        assert history[2].operation_type == "op_7"

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, executor: WriteOperationExecutor) -> None:
        """Test that tenants cannot see each other's operations."""
        tenant_a = uuid.uuid4()
        tenant_b = uuid.uuid4()

        # Create operations for both tenants
        op_a = WriteOperation(
            tenant_id=tenant_a,
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="op_a",
            params={},
            description="Tenant A",
            risk_level=RiskLevel.HIGH,
        )
        op_b = WriteOperation(
            tenant_id=tenant_b,
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="op_b",
            params={},
            description="Tenant B",
            risk_level=RiskLevel.HIGH,
        )

        await executor.propose(op_a)
        await executor.propose(op_b)

        # Tenant A should only see their operation
        pending_a = await executor.get_pending(tenant_id=tenant_a)
        assert len(pending_a) == 1
        assert pending_a[0].tenant_id == tenant_a

        # Tenant B should only see their operation
        pending_b = await executor.get_pending(tenant_id=tenant_b)
        assert len(pending_b) == 1
        assert pending_b[0].tenant_id == tenant_b
