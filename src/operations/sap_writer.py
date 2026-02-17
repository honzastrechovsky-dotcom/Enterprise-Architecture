"""SAP Write Connector with HITL approval.

Provides write operations to SAP ERP via approval workflow:
- create_purchase_request: Create purchase requisitions
- update_inventory: Update material stock levels
- create_goods_receipt: Record goods receipts against POs

All write operations:
1. Validate parameters
2. Create WriteOperation proposal
3. Return proposal for approval workflow
4. Do NOT execute directly

Execution happens via WriteOperationExecutor after approval.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from src.connectors.sap import SAPConnector
from src.operations.write_framework import RiskLevel, WriteOperation

log = structlog.get_logger(__name__)


class SAPWriteConnector(SAPConnector):
    """SAP write connector with HITL approval.

    Extends SAPConnector (read-only) to add write operations.
    All write operations return WriteOperation proposals.
    """

    async def create_purchase_request(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> WriteOperation:
        """Propose creation of a purchase request in SAP.

        Parameters:
            material_id: Material number
            quantity: Quantity to order
            plant_id: Plant ID
            delivery_date: Requested delivery date (ISO format)
            justification: (optional) Reason for request

        Returns:
            WriteOperation proposal in PROPOSED state

        Raises:
            ValueError: If parameters are invalid
        """
        # Validate required parameters
        required = ["material_id", "quantity", "plant_id", "delivery_date"]
        for field in required:
            if field not in params:
                raise ValueError(f"Missing required parameter: {field}")

        # Validate quantity is positive
        quantity = params.get("quantity", 0)
        if not isinstance(quantity, (int, float)) or quantity <= 0:
            raise ValueError("quantity must be a positive number")

        # Generate human-readable description
        description = (
            f"Create purchase request for {quantity} units of "
            f"material {params['material_id']} in plant {params['plant_id']}"
        )
        if "justification" in params:
            description += f" - {params['justification']}"

        # Create operation proposal
        operation = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="sap",
            operation_type="create_purchase_request",
            params=params,
            description=description,
            risk_level=RiskLevel.HIGH,  # Financial commitment
        )

        # SAP write operations require MFA
        operation.requires_mfa = True

        log.info(
            "sap.create_purchase_request_proposed",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            material_id=params["material_id"],
            quantity=quantity,
        )

        return operation

    async def update_inventory(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> WriteOperation:
        """Propose inventory update in SAP.

        Parameters:
            material_id: Material number
            plant_id: Plant ID
            storage_location: Storage location code
            quantity_delta: Quantity change (positive = add, negative = consume)
            movement_type: SAP movement type code (e.g., "261" for goods issue)
            reason: Reason for inventory adjustment

        Returns:
            WriteOperation proposal

        Raises:
            ValueError: If parameters are invalid
        """
        # Validate required parameters
        required = [
            "material_id",
            "plant_id",
            "storage_location",
            "quantity_delta",
            "movement_type",
        ]
        for field in required:
            if field not in params:
                raise ValueError(f"Missing required parameter: {field}")

        quantity_delta = params["quantity_delta"]
        action = "add" if quantity_delta > 0 else "consume"

        description = (
            f"Update inventory: {action} {abs(quantity_delta)} units of "
            f"material {params['material_id']} at {params['plant_id']}/{params['storage_location']} "
            f"(movement type {params['movement_type']})"
        )

        operation = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="sap",
            operation_type="update_inventory",
            params=params,
            description=description,
            risk_level=RiskLevel.HIGH,  # Affects stock levels
        )

        operation.requires_mfa = True

        log.info(
            "sap.update_inventory_proposed",
            tenant_id=str(tenant_id),
            material_id=params["material_id"],
            quantity_delta=quantity_delta,
        )

        return operation

    async def create_goods_receipt(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> WriteOperation:
        """Propose goods receipt creation in SAP.

        Parameters:
            po_number: Purchase order number
            material_id: Material number
            quantity: Quantity received
            plant_id: Plant ID
            storage_location: Storage location code
            delivery_note: (optional) Delivery note number

        Returns:
            WriteOperation proposal

        Raises:
            ValueError: If parameters are invalid
        """
        # Validate required parameters
        required = ["po_number", "material_id", "quantity", "plant_id", "storage_location"]
        for field in required:
            if field not in params:
                raise ValueError(f"Missing required parameter: {field}")

        # Validate quantity is positive
        quantity = params.get("quantity", 0)
        if not isinstance(quantity, (int, float)) or quantity <= 0:
            raise ValueError("quantity must be a positive number")

        description = (
            f"Create goods receipt for PO {params['po_number']}: "
            f"{quantity} units of material {params['material_id']} "
            f"to {params['plant_id']}/{params['storage_location']}"
        )

        operation = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="sap",
            operation_type="create_goods_receipt",
            params=params,
            description=description,
            risk_level=RiskLevel.MEDIUM,  # Lower risk than PR creation
        )

        operation.requires_mfa = True

        log.info(
            "sap.create_goods_receipt_proposed",
            tenant_id=str(tenant_id),
            po_number=params["po_number"],
            quantity=quantity,
        )

        return operation
