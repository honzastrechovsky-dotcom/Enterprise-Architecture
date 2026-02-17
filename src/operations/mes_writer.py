"""MES Write Connector with HITL approval.

Provides write operations to MES (Manufacturing Execution System):
- create_work_order: Create production work orders
- update_production_status: Update order status
- report_quality_issue: Report quality defects
- log_downtime_event: Log machine downtime

All write operations follow approval workflow.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from src.connectors.mes import MESConnector
from src.operations.write_framework import RiskLevel, WriteOperation

log = structlog.get_logger(__name__)


class MESWriteConnector(MESConnector):
    """MES write connector with HITL approval.

    Extends MESConnector (read-only) to add write operations.
    All write operations return WriteOperation proposals.
    """

    async def create_work_order(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> WriteOperation:
        """Propose creation of a production work order.

        Parameters:
            material_id: Material to produce
            quantity_planned: Planned production quantity
            work_center: Work center ID
            priority: Priority level (1-5)
            start_date: Planned start date (ISO format)
            due_date: Due date (ISO format)

        Returns:
            WriteOperation proposal

        Raises:
            ValueError: If parameters are invalid
        """
        # Validate required parameters
        required = [
            "material_id",
            "quantity_planned",
            "work_center",
            "priority",
            "start_date",
        ]
        for field in required:
            if field not in params:
                raise ValueError(f"Missing required parameter: {field}")

        # Validate quantity is positive
        quantity = params.get("quantity_planned", 0)
        if not isinstance(quantity, (int, float)) or quantity <= 0:
            raise ValueError("quantity_planned must be a positive number")

        description = (
            f"Create work order for {quantity} units of material {params['material_id']} "
            f"at work center {params['work_center']} (priority {params['priority']})"
        )

        operation = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="mes",
            operation_type="create_work_order",
            params=params,
            description=description,
            risk_level=RiskLevel.HIGH,  # Impacts production schedule
        )

        operation.requires_mfa = True

        log.info(
            "mes.create_work_order_proposed",
            tenant_id=str(tenant_id),
            material_id=params["material_id"],
            quantity=quantity,
        )

        return operation

    async def update_production_status(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> WriteOperation:
        """Propose update to production order status.

        Parameters:
            order_id: Work order ID
            new_status: New status (planned, released, in_progress, completed, cancelled)
            quantity_produced: (optional) Actual quantity produced
            notes: (optional) Status change notes

        Returns:
            WriteOperation proposal

        Raises:
            ValueError: If parameters are invalid
        """
        # Validate required parameters
        required = ["order_id", "new_status"]
        for field in required:
            if field not in params:
                raise ValueError(f"Missing required parameter: {field}")

        # Validate status
        valid_statuses = ["planned", "released", "in_progress", "completed", "cancelled"]
        if params["new_status"] not in valid_statuses:
            raise ValueError(
                f"Invalid status: {params['new_status']}. Must be one of {valid_statuses}"
            )

        description = (
            f"Update production order {params['order_id']} status to {params['new_status']}"
        )
        if "quantity_produced" in params:
            description += f" ({params['quantity_produced']} units produced)"

        # Cancelling is higher risk than other status changes
        risk = RiskLevel.HIGH if params["new_status"] == "cancelled" else RiskLevel.MEDIUM

        operation = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="mes",
            operation_type="update_production_status",
            params=params,
            description=description,
            risk_level=risk,
        )

        # Only cancellations require MFA
        operation.requires_mfa = (params["new_status"] == "cancelled")

        log.info(
            "mes.update_production_status_proposed",
            tenant_id=str(tenant_id),
            order_id=params["order_id"],
            new_status=params["new_status"],
        )

        return operation

    async def report_quality_issue(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> WriteOperation:
        """Propose reporting a quality issue.

        Parameters:
            order_id: Production order ID
            material_id: Material ID
            defect_count: Number of defective units
            defect_types: List of defect type codes
            sample_size: Sample size inspected
            inspector_id: Inspector user ID
            notes: Detailed notes

        Returns:
            WriteOperation proposal

        Raises:
            ValueError: If parameters are invalid
        """
        # Validate required parameters
        required = [
            "order_id",
            "material_id",
            "defect_count",
            "defect_types",
            "sample_size",
        ]
        for field in required:
            if field not in params:
                raise ValueError(f"Missing required parameter: {field}")

        defect_count = params["defect_count"]
        sample_size = params["sample_size"]

        description = (
            f"Report quality issue for order {params['order_id']}: "
            f"{defect_count}/{sample_size} defective units "
            f"(defect types: {', '.join(params['defect_types'])})"
        )

        operation = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="mes",
            operation_type="report_quality_issue",
            params=params,
            description=description,
            risk_level=RiskLevel.MEDIUM,  # Quality data is important but not system-critical
        )

        log.info(
            "mes.report_quality_issue_proposed",
            tenant_id=str(tenant_id),
            order_id=params["order_id"],
            defect_count=defect_count,
        )

        return operation

    async def log_downtime_event(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> WriteOperation:
        """Propose logging a machine downtime event.

        Parameters:
            machine_id: Machine ID
            start_time: Downtime start (ISO format)
            end_time: (optional) Downtime end (ISO format)
            reason_code: Downtime reason code
            reason_description: Human-readable description
            order_id: (optional) Affected order ID

        Returns:
            WriteOperation proposal

        Raises:
            ValueError: If parameters are invalid
        """
        # Validate required parameters
        required = ["machine_id", "start_time", "reason_code", "reason_description"]
        for field in required:
            if field not in params:
                raise ValueError(f"Missing required parameter: {field}")

        description = (
            f"Log downtime for machine {params['machine_id']}: "
            f"{params['reason_description']} (code: {params['reason_code']})"
        )

        operation = WriteOperation(
            tenant_id=tenant_id,
            user_id=user_id,
            connector="mes",
            operation_type="log_downtime_event",
            params=params,
            description=description,
            risk_level=RiskLevel.LOW,  # Logging is low risk
        )

        log.info(
            "mes.log_downtime_event_proposed",
            tenant_id=str(tenant_id),
            machine_id=params["machine_id"],
            reason_code=params["reason_code"],
        )

        return operation
