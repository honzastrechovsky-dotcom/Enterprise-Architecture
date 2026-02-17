"""Tests for MES Write Connector.

Tests for write operations to MES via HITL approval workflow.
"""

from __future__ import annotations

import uuid

import pytest

from src.operations.mes_writer import MESWriteConnector
from src.operations.write_framework import RiskLevel, OperationStatus, WriteOperation
from src.connectors.base import ConnectorConfig, AuthType


class TestMESWriteConnector:
    """Tests for MESWriteConnector."""

    @pytest.fixture
    def config(self) -> ConnectorConfig:
        """Fixture providing MES connector config."""
        return ConnectorConfig(
            name="mes_write",
            endpoint="https://mes.test.com/api/v1",
            auth_type=AuthType.API_KEY,
            auth_params={"api_key": "test-key-123"},
        )

    @pytest.fixture
    async def connector(self, config: ConnectorConfig) -> MESWriteConnector:
        """Fixture providing MES write connector instance."""
        conn = MESWriteConnector(config)
        async with conn:
            yield conn

    @pytest.mark.asyncio
    async def test_create_work_order_proposes_operation(
        self, connector: MESWriteConnector
    ) -> None:
        """Test that create_work_order returns a WriteOperation proposal."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        params = {
            "material_id": "MAT-001",
            "quantity_planned": 1000,
            "work_center": "WC-101",
            "priority": 3,
            "start_date": "2024-03-01T08:00:00Z",
            "due_date": "2024-03-05T17:00:00Z",
        }

        operation = await connector.create_work_order(
            tenant_id=tenant_id,
            user_id=user_id,
            params=params,
        )

        assert isinstance(operation, WriteOperation)
        assert operation.connector == "mes"
        assert operation.operation_type == "create_work_order"
        assert operation.risk_level == RiskLevel.HIGH
        assert operation.requires_mfa is True

    @pytest.mark.asyncio
    async def test_update_production_status_proposes_operation(
        self, connector: MESWriteConnector
    ) -> None:
        """Test that update_production_status returns a WriteOperation proposal."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        params = {
            "order_id": "WO-12345",
            "new_status": "in_progress",
            "quantity_produced": 500,
            "notes": "Started production",
        }

        operation = await connector.update_production_status(
            tenant_id=tenant_id,
            user_id=user_id,
            params=params,
        )

        assert operation.operation_type == "update_production_status"
        assert operation.risk_level == RiskLevel.MEDIUM
        assert operation.requires_mfa is False  # Not a cancellation

    @pytest.mark.asyncio
    async def test_cancel_status_requires_mfa(
        self, connector: MESWriteConnector
    ) -> None:
        """Test that cancelling an order requires MFA."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        params = {
            "order_id": "WO-12345",
            "new_status": "cancelled",
            "notes": "Material shortage",
        }

        operation = await connector.update_production_status(
            tenant_id=tenant_id,
            user_id=user_id,
            params=params,
        )

        assert operation.risk_level == RiskLevel.HIGH
        assert operation.requires_mfa is True  # Cancellations require MFA

    @pytest.mark.asyncio
    async def test_report_quality_issue_proposes_operation(
        self, connector: MESWriteConnector
    ) -> None:
        """Test that report_quality_issue returns a WriteOperation proposal."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        params = {
            "order_id": "WO-12345",
            "material_id": "MAT-001",
            "defect_count": 15,
            "defect_types": ["scratch", "dimension_error"],
            "sample_size": 100,
            "notes": "Surface defects on batch 5",
        }

        operation = await connector.report_quality_issue(
            tenant_id=tenant_id,
            user_id=user_id,
            params=params,
        )

        assert operation.operation_type == "report_quality_issue"
        assert operation.risk_level == RiskLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_log_downtime_event_is_low_risk(
        self, connector: MESWriteConnector
    ) -> None:
        """Test that logging downtime is LOW risk and auto-approved."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        params = {
            "machine_id": "MACH-101",
            "start_time": "2024-03-01T10:30:00Z",
            "end_time": "2024-03-01T12:00:00Z",
            "reason_code": "MAINTENANCE",
            "reason_description": "Scheduled preventive maintenance",
        }

        operation = await connector.log_downtime_event(
            tenant_id=tenant_id,
            user_id=user_id,
            params=params,
        )

        assert operation.operation_type == "log_downtime_event"
        assert operation.risk_level == RiskLevel.LOW
        assert operation.requires_approval is False  # Auto-approved

    @pytest.mark.asyncio
    async def test_parameter_validation(
        self, connector: MESWriteConnector
    ) -> None:
        """Test that invalid parameters raise ValueError."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        # Missing required field
        params = {
            "material_id": "MAT-001",
            # Missing quantity_planned
            "work_center": "WC-101",
            "priority": 3,
            "start_date": "2024-03-01T08:00:00Z",
        }

        with pytest.raises(ValueError, match="quantity_planned"):
            await connector.create_work_order(
                tenant_id=tenant_id,
                user_id=user_id,
                params=params,
            )

    @pytest.mark.asyncio
    async def test_invalid_status_raises_error(
        self, connector: MESWriteConnector
    ) -> None:
        """Test that invalid status values raise ValueError."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        params = {
            "order_id": "WO-12345",
            "new_status": "invalid_status",  # Invalid
        }

        with pytest.raises(ValueError, match="Invalid status"):
            await connector.update_production_status(
                tenant_id=tenant_id,
                user_id=user_id,
                params=params,
            )
