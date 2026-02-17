"""MES (Manufacturing Execution System) connector - READ-ONLY real-time data.

Provides read-only access to manufacturing execution data:
- Production Orders
- Machine Status (real-time)
- Quality Reports
- Downtime Events

All methods:
- Enforce tenant_id scoping
- Return normalized dataclasses
- Include classification tagging
- Support real-time polling with configurable intervals

Reference deployment: Custom MES with REST API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from src.connectors.base import (
    BaseConnector,
    ConnectorResult,
    ConnectorStatus,
)

log = structlog.get_logger(__name__)


class MachineStatus(StrEnum):
    """Machine operational status."""

    RUNNING = "running"
    IDLE = "idle"
    MAINTENANCE = "maintenance"
    DOWN = "down"
    UNKNOWN = "unknown"


class OrderStatus(StrEnum):
    """Production order status."""

    PLANNED = "planned"
    RELEASED = "released"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class ProductionOrder:
    """Normalized production order from MES."""

    order_id: str
    material_id: str
    material_description: str
    quantity_planned: float
    quantity_produced: float
    unit_of_measure: str
    status: OrderStatus
    priority: int
    start_date: datetime | None
    end_date: datetime | None
    work_center: str
    classification: str = "class_ii"  # Data Classification: Internal use


@dataclass
class MachineStatusData:
    """Real-time machine status from MES."""

    machine_id: str
    machine_name: str
    work_center: str
    status: MachineStatus
    current_order_id: str | None
    production_rate: float  # Units per hour
    utilization_percent: float
    last_event_time: datetime
    alarms_active: list[str]
    classification: str = "class_ii"  # Data Classification: Internal use


@dataclass
class QualityReport:
    """Quality inspection report from MES."""

    report_id: str
    order_id: str
    material_id: str
    inspector_id: str
    inspection_date: datetime
    passed: bool
    defect_count: int
    defect_types: list[str]
    sample_size: int
    notes: str
    classification: str = "class_iii"  # Data Classification: Quality data (sensitive)


@dataclass
class DowntimeEvent:
    """Machine downtime event from MES."""

    event_id: str
    machine_id: str
    start_time: datetime
    end_time: datetime | None
    duration_minutes: float | None
    reason_code: str
    reason_description: str
    order_id: str | None
    resolved_by: str | None
    classification: str = "class_ii"  # Data Classification: Internal use


class MESConnector(BaseConnector):
    """MES connector using REST API.

    All operations are READ-ONLY. Real-time data with configurable polling.

    Configuration:
        - endpoint: MES REST API base URL
        - auth_type: API_KEY or BEARER
        - auth_params: api_key or token
    """

    async def _execute_request(
        self,
        operation: str,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Execute MES-specific request."""
        if operation == "get_production_orders":
            return await self._get_production_orders(tenant_id, params)
        elif operation == "get_machine_status":
            return await self._get_machine_status(tenant_id, params)
        elif operation == "get_quality_reports":
            return await self._get_quality_reports(tenant_id, params)
        elif operation == "get_downtime_events":
            return await self._get_downtime_events(tenant_id, params)
        else:
            return ConnectorResult(
                success=False,
                error=f"Unknown MES operation: {operation}",
            )

    async def health_check(self) -> ConnectorStatus:
        """Check MES system availability via health endpoint."""
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()

            # Standard health check endpoint
            response = await client.get("/health", headers=headers, timeout=10.0)

            if response.status_code == 200:
                self._status = ConnectorStatus.HEALTHY
                log.info("mes.health_check_ok", endpoint=self.config.endpoint)
            elif response.status_code in (500, 502, 503, 504):
                self._status = ConnectorStatus.UNAVAILABLE
                log.warning("mes.health_check_unavailable", status=response.status_code)
            else:
                self._status = ConnectorStatus.DEGRADED
                log.warning("mes.health_check_degraded", status=response.status_code)

        except Exception as exc:
            self._status = ConnectorStatus.UNAVAILABLE
            log.error("mes.health_check_failed", error=str(exc))

        return self._status

    async def _get_production_orders(
        self,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Retrieve production orders from MES.

        Params:
            - status (optional): Filter by order status
            - work_center (optional): Filter by work center
            - from_date (optional): Filter by start date
            - limit (optional): Max results (default 100)

        Returns:
            ConnectorResult with list[ProductionOrder]
        """
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()

            query_params: dict[str, Any] = {
                "limit": params.get("limit", 100),
            }
            if "status" in params:
                query_params["status"] = params["status"]
            if "work_center" in params:
                query_params["work_center"] = params["work_center"]
            if "from_date" in params:
                query_params["from_date"] = params["from_date"]

            response = await client.get(
                "/api/v1/production-orders",
                headers=headers,
                params=query_params,
            )

            if response.status_code != 200:
                return ConnectorResult(
                    success=False,
                    error=f"MES API error: {response.status_code} - {response.text}",
                )

            data = response.json()
            orders_data = data.get("orders", [])

            production_orders = [
                self._map_production_order(order_data) for order_data in orders_data
            ]

            return ConnectorResult(
                success=True,
                data=production_orders,
                metadata={
                    "count": len(production_orders),
                    "tenant_id": str(tenant_id),
                },
                classification="class_ii",
            )

        except Exception as exc:
            log.error("mes.get_production_orders_failed", error=str(exc))
            return ConnectorResult(success=False, error=str(exc))

    async def _get_machine_status(
        self,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Retrieve real-time machine status from MES.

        Params:
            - machine_id (optional): Filter by machine ID
            - work_center (optional): Filter by work center
            - status (optional): Filter by status

        Returns:
            ConnectorResult with list[MachineStatusData]
        """
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()

            query_params: dict[str, Any] = {}
            if "machine_id" in params:
                query_params["machine_id"] = params["machine_id"]
            if "work_center" in params:
                query_params["work_center"] = params["work_center"]
            if "status" in params:
                query_params["status"] = params["status"]

            response = await client.get(
                "/api/v1/machines/status",
                headers=headers,
                params=query_params,
            )

            if response.status_code != 200:
                return ConnectorResult(
                    success=False,
                    error=f"MES API error: {response.status_code}",
                )

            data = response.json()
            machines_data = data.get("machines", [])

            machine_statuses = [
                self._map_machine_status(machine_data) for machine_data in machines_data
            ]

            return ConnectorResult(
                success=True,
                data=machine_statuses,
                metadata={
                    "count": len(machine_statuses),
                    "tenant_id": str(tenant_id),
                    "timestamp": data.get("timestamp", datetime.now(UTC).isoformat()),
                },
                classification="class_ii",
            )

        except Exception as exc:
            log.error("mes.get_machine_status_failed", error=str(exc))
            return ConnectorResult(success=False, error=str(exc))

    async def _get_quality_reports(
        self,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Retrieve quality inspection reports from MES.

        Params:
            - order_id (optional): Filter by production order
            - from_date (optional): Filter by inspection date
            - passed (optional): Filter by pass/fail status
            - limit (optional): Max results (default 50)

        Returns:
            ConnectorResult with list[QualityReport]
        """
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()

            query_params: dict[str, Any] = {
                "limit": params.get("limit", 50),
            }
            if "order_id" in params:
                query_params["order_id"] = params["order_id"]
            if "from_date" in params:
                query_params["from_date"] = params["from_date"]
            if "passed" in params:
                query_params["passed"] = str(params["passed"]).lower()

            response = await client.get(
                "/api/v1/quality-reports",
                headers=headers,
                params=query_params,
            )

            if response.status_code != 200:
                return ConnectorResult(
                    success=False,
                    error=f"MES API error: {response.status_code}",
                )

            data = response.json()
            reports_data = data.get("reports", [])

            quality_reports = [
                self._map_quality_report(report_data) for report_data in reports_data
            ]

            return ConnectorResult(
                success=True,
                data=quality_reports,
                metadata={
                    "count": len(quality_reports),
                    "tenant_id": str(tenant_id),
                },
                classification="class_iii",  # Quality data sensitive
            )

        except Exception as exc:
            log.error("mes.get_quality_reports_failed", error=str(exc))
            return ConnectorResult(success=False, error=str(exc))

    async def _get_downtime_events(
        self,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Retrieve machine downtime events from MES.

        Params:
            - machine_id (optional): Filter by machine
            - from_date (optional): Filter by start date
            - resolved (optional): Filter by resolution status
            - limit (optional): Max results (default 100)

        Returns:
            ConnectorResult with list[DowntimeEvent]
        """
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()

            query_params: dict[str, Any] = {
                "limit": params.get("limit", 100),
            }
            if "machine_id" in params:
                query_params["machine_id"] = params["machine_id"]
            if "from_date" in params:
                query_params["from_date"] = params["from_date"]
            if "resolved" in params:
                query_params["resolved"] = str(params["resolved"]).lower()

            response = await client.get(
                "/api/v1/downtime-events",
                headers=headers,
                params=query_params,
            )

            if response.status_code != 200:
                return ConnectorResult(
                    success=False,
                    error=f"MES API error: {response.status_code}",
                )

            data = response.json()
            events_data = data.get("events", [])

            downtime_events = [
                self._map_downtime_event(event_data) for event_data in events_data
            ]

            return ConnectorResult(
                success=True,
                data=downtime_events,
                metadata={
                    "count": len(downtime_events),
                    "tenant_id": str(tenant_id),
                },
                classification="class_ii",
            )

        except Exception as exc:
            log.error("mes.get_downtime_events_failed", error=str(exc))
            return ConnectorResult(success=False, error=str(exc))

    # --------------------------------------------------------------------- #
    # Mapping functions: MES REST API responses -> normalized dataclasses
    # --------------------------------------------------------------------- #

    def _map_production_order(self, data: dict[str, Any]) -> ProductionOrder:
        """Map MES production order to normalized dataclass."""
        return ProductionOrder(
            order_id=data.get("order_id", ""),
            material_id=data.get("material_id", ""),
            material_description=data.get("material_description", ""),
            quantity_planned=float(data.get("quantity_planned", 0)),
            quantity_produced=float(data.get("quantity_produced", 0)),
            unit_of_measure=data.get("unit_of_measure", ""),
            status=OrderStatus(data.get("status", "unknown")),
            priority=int(data.get("priority", 0)),
            start_date=self._parse_iso_datetime(data.get("start_date")),
            end_date=self._parse_iso_datetime(data.get("end_date")),
            work_center=data.get("work_center", ""),
        )

    def _map_machine_status(self, data: dict[str, Any]) -> MachineStatusData:
        """Map MES machine status to normalized dataclass."""
        return MachineStatusData(
            machine_id=data.get("machine_id", ""),
            machine_name=data.get("machine_name", ""),
            work_center=data.get("work_center", ""),
            status=MachineStatus(data.get("status", "unknown")),
            current_order_id=data.get("current_order_id"),
            production_rate=float(data.get("production_rate", 0)),
            utilization_percent=float(data.get("utilization_percent", 0)),
            last_event_time=self._parse_iso_datetime(data.get("last_event_time"))
            or datetime.now(UTC),
            alarms_active=data.get("alarms_active", []),
        )

    def _map_quality_report(self, data: dict[str, Any]) -> QualityReport:
        """Map MES quality report to normalized dataclass."""
        return QualityReport(
            report_id=data.get("report_id", ""),
            order_id=data.get("order_id", ""),
            material_id=data.get("material_id", ""),
            inspector_id=data.get("inspector_id", ""),
            inspection_date=self._parse_iso_datetime(data.get("inspection_date"))
            or datetime.now(UTC),
            passed=bool(data.get("passed", False)),
            defect_count=int(data.get("defect_count", 0)),
            defect_types=data.get("defect_types", []),
            sample_size=int(data.get("sample_size", 0)),
            notes=data.get("notes", ""),
        )

    def _map_downtime_event(self, data: dict[str, Any]) -> DowntimeEvent:
        """Map MES downtime event to normalized dataclass."""
        start_time = self._parse_iso_datetime(data.get("start_time")) or datetime.now(UTC)
        end_time = self._parse_iso_datetime(data.get("end_time"))

        duration_minutes = None
        if end_time:
            duration_minutes = (end_time - start_time).total_seconds() / 60

        return DowntimeEvent(
            event_id=data.get("event_id", ""),
            machine_id=data.get("machine_id", ""),
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            reason_code=data.get("reason_code", ""),
            reason_description=data.get("reason_description", ""),
            order_id=data.get("order_id"),
            resolved_by=data.get("resolved_by"),
        )

    def _parse_iso_datetime(self, value: Any) -> datetime | None:
        """Parse ISO 8601 datetime string.

        Returns datetime object or None if parsing fails.
        """
        if not value:
            return None

        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
