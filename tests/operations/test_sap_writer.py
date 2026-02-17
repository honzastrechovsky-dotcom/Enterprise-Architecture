"""Tests for SAP Write Connector.

Tests for write operations to SAP via HITL approval workflow.
"""

from __future__ import annotations

import uuid

import pytest

from src.operations.sap_writer import SAPWriteConnector
from src.operations.write_framework import RiskLevel, OperationStatus, WriteOperation
from src.connectors.base import ConnectorConfig, AuthType


class TestSAPWriteConnector:
    """Tests for SAPWriteConnector."""

    @pytest.fixture
    def config(self) -> ConnectorConfig:
        """Fixture providing SAP connector config."""
        return ConnectorConfig(
            name="sap_write",
            endpoint="https://sap.test.com:8000/sap/opu/odata/sap",
            auth_type=AuthType.BASIC,
            auth_params={"username": "test", "password": "test"},
        )

    @pytest.fixture
    async def connector(self, config: ConnectorConfig) -> SAPWriteConnector:
        """Fixture providing SAP write connector instance."""
        conn = SAPWriteConnector(config)
        async with conn:
            yield conn

    @pytest.mark.asyncio
    async def test_create_purchase_request_proposes_operation(
        self, connector: SAPWriteConnector
    ) -> None:
        """Test that create_purchase_request returns a WriteOperation proposal."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        params = {
            "material_id": "MAT-12345",
            "quantity": 100,
            "plant_id": "1000",
            "delivery_date": "2024-03-01",
            "justification": "Stock replenishment",
        }

        operation = await connector.create_purchase_request(
            tenant_id=tenant_id,
            user_id=user_id,
            params=params,
        )

        # Should return WriteOperation in PROPOSED state
        assert isinstance(operation, WriteOperation)
        assert operation.connector == "sap"
        assert operation.operation_type == "create_purchase_request"
        assert operation.params == params
        assert operation.risk_level == RiskLevel.HIGH
        assert operation.requires_approval is True
        assert operation.requires_mfa is True  # SAP writes require MFA
        assert operation.status == OperationStatus.PROPOSED
        assert "purchase request" in operation.description.lower()

    @pytest.mark.asyncio
    async def test_create_purchase_request_validates_params(
        self, connector: SAPWriteConnector
    ) -> None:
        """Test parameter validation before proposal."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        # Missing required field
        params = {
            "material_id": "MAT-001",
            # Missing quantity
            "plant_id": "1000",
        }

        with pytest.raises(ValueError, match="quantity"):
            await connector.create_purchase_request(
                tenant_id=tenant_id,
                user_id=user_id,
                params=params,
            )

    @pytest.mark.asyncio
    async def test_update_inventory_proposes_operation(
        self, connector: SAPWriteConnector
    ) -> None:
        """Test that update_inventory returns a WriteOperation proposal."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        params = {
            "material_id": "MAT-001",
            "plant_id": "1000",
            "storage_location": "SL01",
            "quantity_delta": -50,  # Negative = consumption
            "movement_type": "261",  # Goods issue
            "reason": "Production consumption",
        }

        operation = await connector.update_inventory(
            tenant_id=tenant_id,
            user_id=user_id,
            params=params,
        )

        assert operation.operation_type == "update_inventory"
        assert operation.risk_level == RiskLevel.HIGH
        assert operation.requires_approval is True
        assert operation.requires_mfa is True

    @pytest.mark.asyncio
    async def test_create_goods_receipt_proposes_operation(
        self, connector: SAPWriteConnector
    ) -> None:
        """Test that create_goods_receipt returns a WriteOperation proposal."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        params = {
            "po_number": "4500001234",
            "material_id": "MAT-001",
            "quantity": 100,
            "plant_id": "1000",
            "storage_location": "SL01",
            "delivery_note": "DN-2024-001",
        }

        operation = await connector.create_goods_receipt(
            tenant_id=tenant_id,
            user_id=user_id,
            params=params,
        )

        assert operation.operation_type == "create_goods_receipt"
        assert operation.risk_level == RiskLevel.MEDIUM  # Lower risk than PR
        assert operation.requires_approval is True
        assert operation.requires_mfa is True

    @pytest.mark.asyncio
    async def test_risk_classification(self, connector: SAPWriteConnector) -> None:
        """Test that different operations have appropriate risk levels."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        # Create Purchase Request - HIGH risk (creates financial commitment)
        pr_op = await connector.create_purchase_request(
            tenant_id=tenant_id,
            user_id=user_id,
            params={
                "material_id": "MAT-001",
                "quantity": 100,
                "plant_id": "1000",
                "delivery_date": "2024-03-01",
            },
        )
        assert pr_op.risk_level == RiskLevel.HIGH

        # Goods Receipt - MEDIUM risk (records receipt, affects inventory)
        gr_op = await connector.create_goods_receipt(
            tenant_id=tenant_id,
            user_id=user_id,
            params={
                "po_number": "4500001234",
                "material_id": "MAT-001",
                "quantity": 50,
                "plant_id": "1000",
                "storage_location": "SL01",
            },
        )
        assert gr_op.risk_level == RiskLevel.MEDIUM

    @pytest.mark.asyncio
    async def test_description_generation(
        self, connector: SAPWriteConnector
    ) -> None:
        """Test that operations generate human-readable descriptions."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        operation = await connector.create_purchase_request(
            tenant_id=tenant_id,
            user_id=user_id,
            params={
                "material_id": "MAT-12345",
                "quantity": 100,
                "plant_id": "1000",
                "delivery_date": "2024-03-01",
                "justification": "Urgent production need",
            },
        )

        # Description should be informative
        desc = operation.description
        assert "MAT-12345" in desc
        assert "100" in desc or "quantity" in desc.lower()
        assert len(desc) > 20  # Reasonably detailed

    @pytest.mark.asyncio
    async def test_tenant_isolation_in_params(
        self, connector: SAPWriteConnector
    ) -> None:
        """Test that tenant_id is correctly embedded in operations."""
        tenant_a = uuid.uuid4()
        tenant_b = uuid.uuid4()

        op_a = await connector.create_purchase_request(
            tenant_id=tenant_a,
            user_id=uuid.uuid4(),
            params={
                "material_id": "MAT-001",
                "quantity": 100,
                "plant_id": "1000",
                "delivery_date": "2024-03-01",
            },
        )

        op_b = await connector.create_purchase_request(
            tenant_id=tenant_b,
            user_id=uuid.uuid4(),
            params={
                "material_id": "MAT-002",
                "quantity": 200,
                "plant_id": "2000",
                "delivery_date": "2024-03-01",
            },
        )

        assert op_a.tenant_id == tenant_a
        assert op_b.tenant_id == tenant_b
        assert op_a.tenant_id != op_b.tenant_id
