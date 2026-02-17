"""SAP ERP connector - READ-ONLY access to enterprise data.

Provides read-only access to SAP systems via OData API:
- Purchase Orders
- Inventory levels
- Cost Centers
- Material Master data

All methods:
- Enforce tenant_id scoping
- Return normalized dataclasses
- Include classification tagging
- Log all access for audit

Reference deployment: SAP S/4HANA with OData v2 API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import structlog

from src.connectors.base import (
    BaseConnector,
    ConnectorResult,
    ConnectorStatus,
)

log = structlog.get_logger(__name__)


@dataclass
class PurchaseOrder:
    """Normalized purchase order from SAP."""

    po_number: str
    vendor_id: str
    vendor_name: str
    total_amount: float
    currency: str
    status: str
    created_date: datetime
    delivery_date: datetime | None
    items: list[PurchaseOrderItem]
    classification: str = "class_iii"  # Data Classification: Business sensitive


@dataclass
class PurchaseOrderItem:
    """Line item in a purchase order."""

    item_number: str
    material_id: str
    description: str
    quantity: float
    unit_price: float
    total_price: float


@dataclass
class InventoryItem:
    """Inventory item from SAP."""

    material_id: str
    description: str
    plant_id: str
    storage_location: str
    quantity_on_hand: float
    unit_of_measure: str
    valuation_class: str
    last_updated: datetime
    classification: str = "class_ii"  # Data Classification: Internal use


@dataclass
class CostCenter:
    """Cost center from SAP controlling."""

    cost_center_id: str
    name: str
    department: str
    responsible_person: str
    budget_allocated: float
    budget_consumed: float
    currency: str
    classification: str = "class_iii"  # Data Classification: Business sensitive


@dataclass
class MaterialMaster:
    """Material master record from SAP."""

    material_id: str
    description: str
    material_type: str
    base_unit_of_measure: str
    gross_weight: float | None
    net_weight: float | None
    weight_unit: str | None
    material_group: str
    procurement_type: str
    classification: str = "class_ii"  # Data Classification: Internal use


class SAPConnector(BaseConnector):
    """SAP ERP connector using OData v2 API.

    All operations are READ-ONLY. Write operations require explicit
    approval workflow via the HITL write framework.

    Configuration:
        - endpoint: SAP OData service root (e.g., https://sap.example.com:8000/sap/opu/odata/sap)
        - auth_type: BASIC or OAUTH2
        - auth_params: username/password or OAuth token
    """

    # Characters that are meaningful in OData $filter expressions and must
    # never appear verbatim in a value that is embedded via string interpolation.
    _ODATA_DANGEROUS_CHARS = frozenset("';()")

    @staticmethod
    def _sanitize_odata_value(value: str) -> str:
        """Reject values containing OData filter injection characters.

        Decodes URL-encoded characters before validation to prevent bypass
        via percent-encoding (e.g., %27 for single quote, %3B for semicolon).

        Raises ValueError if the value contains any of: ' ; ( )
        These characters allow an attacker to break out of the string literal
        and inject arbitrary OData filter clauses.

        Args:
            value: User-supplied filter value to validate.

        Returns:
            The decoded, validated value.

        Raises:
            ValueError: If the value contains dangerous characters.
        """
        # Decode URL-encoded characters first to prevent bypass
        decoded = unquote(value)
        dangerous = SAPConnector._ODATA_DANGEROUS_CHARS & set(decoded)
        if dangerous:
            raise ValueError(
                f"OData filter value contains disallowed characters: "
                f"{sorted(dangerous)!r}. Value rejected to prevent injection."
            )
        # Also check the original value for encoded forms of dangerous chars
        if any(f"%{ord(c):02x}" in value.lower() for c in SAPConnector._ODATA_DANGEROUS_CHARS):
            raise ValueError(
                "OData filter value contains encoded disallowed characters"
            )
        return decoded  # Return the decoded, validated value

    async def _execute_request(
        self,
        operation: str,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Execute SAP-specific request."""
        # Route to operation-specific handlers
        if operation == "get_purchase_orders":
            return await self._get_purchase_orders(tenant_id, params)
        elif operation == "get_inventory":
            return await self._get_inventory(tenant_id, params)
        elif operation == "get_cost_centers":
            return await self._get_cost_centers(tenant_id, params)
        elif operation == "get_material_master":
            return await self._get_material_master(tenant_id, params)
        else:
            return ConnectorResult(
                success=False,
                error=f"Unknown SAP operation: {operation}",
            )

    async def health_check(self) -> ConnectorStatus:
        """Check SAP system availability via service metadata endpoint."""
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()

            # OData v2 metadata endpoint
            response = await client.get("/$metadata", headers=headers, timeout=10.0)

            if response.status_code == 200:
                self._status = ConnectorStatus.HEALTHY
                log.info("sap.health_check_ok", endpoint=self.config.endpoint)
            elif response.status_code in (500, 502, 503, 504):
                self._status = ConnectorStatus.UNAVAILABLE
                log.warning("sap.health_check_unavailable", status=response.status_code)
            else:
                self._status = ConnectorStatus.DEGRADED
                log.warning("sap.health_check_degraded", status=response.status_code)

        except Exception as exc:
            self._status = ConnectorStatus.UNAVAILABLE
            log.error("sap.health_check_failed", error=str(exc))

        return self._status

    async def _get_purchase_orders(
        self,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Retrieve purchase orders from SAP.

        Params:
            - vendor_id (optional): Filter by vendor
            - status (optional): Filter by status
            - from_date (optional): Filter by creation date
            - top (optional): Max results (default 50)

        Returns:
            ConnectorResult with list[PurchaseOrder]
        """
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()
            headers["Accept"] = "application/json"

            # Build OData query - sanitize all user-supplied values (C6)
            filters = []
            if "vendor_id" in params:
                filters.append(f"Vendor eq '{self._sanitize_odata_value(str(params['vendor_id']))}'")
            if "status" in params:
                filters.append(f"Status eq '{self._sanitize_odata_value(str(params['status']))}'")
            if "from_date" in params:
                filters.append(
                    f"CreatedDate ge datetime'{self._sanitize_odata_value(str(params['from_date']))}'"
                )

            query_params = {
                "$top": params.get("top", 50),
                "$expand": "Items",
            }
            if filters:
                query_params["$filter"] = " and ".join(filters)

            # Example: /PurchaseOrderSet
            response = await client.get(
                "/PurchaseOrderSet",
                headers=headers,
                params=query_params,
            )

            if response.status_code != 200:
                return ConnectorResult(
                    success=False,
                    error=f"SAP API error: {response.status_code} - {response.text}",
                )

            data = response.json()
            results = data.get("d", {}).get("results", [])

            # Map to normalized dataclass
            purchase_orders = [
                self._map_purchase_order(po_data) for po_data in results
            ]

            return ConnectorResult(
                success=True,
                data=purchase_orders,
                metadata={
                    "count": len(purchase_orders),
                    "tenant_id": str(tenant_id),
                },
                classification="class_iii",  # Business sensitive
            )

        except Exception as exc:
            log.error("sap.get_purchase_orders_failed", error=str(exc))
            return ConnectorResult(success=False, error=str(exc))

    async def _get_inventory(
        self,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Retrieve inventory data from SAP.

        Params:
            - plant_id (optional): Filter by plant
            - material_id (optional): Filter by material
            - top (optional): Max results (default 100)

        Returns:
            ConnectorResult with list[InventoryItem]
        """
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()
            headers["Accept"] = "application/json"

            # Sanitize user-supplied filter values (C6)
            filters = []
            if "plant_id" in params:
                filters.append(f"Plant eq '{self._sanitize_odata_value(str(params['plant_id']))}'")
            if "material_id" in params:
                filters.append(
                    f"Material eq '{self._sanitize_odata_value(str(params['material_id']))}'"
                )

            query_params = {"$top": params.get("top", 100)}
            if filters:
                query_params["$filter"] = " and ".join(filters)

            response = await client.get(
                "/MaterialStockSet",
                headers=headers,
                params=query_params,
            )

            if response.status_code != 200:
                return ConnectorResult(
                    success=False,
                    error=f"SAP API error: {response.status_code}",
                )

            data = response.json()
            results = data.get("d", {}).get("results", [])

            inventory_items = [
                self._map_inventory_item(item_data) for item_data in results
            ]

            return ConnectorResult(
                success=True,
                data=inventory_items,
                metadata={
                    "count": len(inventory_items),
                    "tenant_id": str(tenant_id),
                },
                classification="class_ii",  # Internal use
            )

        except Exception as exc:
            log.error("sap.get_inventory_failed", error=str(exc))
            return ConnectorResult(success=False, error=str(exc))

    async def _get_cost_centers(
        self,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Retrieve cost center data from SAP Controlling.

        Params:
            - department (optional): Filter by department
            - top (optional): Max results (default 50)

        Returns:
            ConnectorResult with list[CostCenter]
        """
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()
            headers["Accept"] = "application/json"

            # Sanitize user-supplied filter values (C6)
            filters = []
            if "department" in params:
                filters.append(
                    f"Department eq '{self._sanitize_odata_value(str(params['department']))}'"
                )

            query_params = {"$top": params.get("top", 50)}
            if filters:
                query_params["$filter"] = " and ".join(filters)

            response = await client.get(
                "/CostCenterSet",
                headers=headers,
                params=query_params,
            )

            if response.status_code != 200:
                return ConnectorResult(
                    success=False,
                    error=f"SAP API error: {response.status_code}",
                )

            data = response.json()
            results = data.get("d", {}).get("results", [])

            cost_centers = [
                self._map_cost_center(cc_data) for cc_data in results
            ]

            return ConnectorResult(
                success=True,
                data=cost_centers,
                metadata={
                    "count": len(cost_centers),
                    "tenant_id": str(tenant_id),
                },
                classification="class_iii",  # Business sensitive
            )

        except Exception as exc:
            log.error("sap.get_cost_centers_failed", error=str(exc))
            return ConnectorResult(success=False, error=str(exc))

    async def _get_material_master(
        self,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Retrieve material master records from SAP.

        Params:
            - material_id (required): Material number
            - material_group (optional): Filter by material group

        Returns:
            ConnectorResult with MaterialMaster or list[MaterialMaster]
        """
        try:
            client = self._get_http_client()
            headers = self._prepare_auth_headers()
            headers["Accept"] = "application/json"

            if "material_id" in params:
                # Single material lookup - sanitize key used in URL (C6)
                safe_material_id = self._sanitize_odata_value(str(params["material_id"]))
                response = await client.get(
                    f"/MaterialSet('{safe_material_id}')",
                    headers=headers,
                )
            else:
                # List materials - sanitize user-supplied filter values (C6)
                filters = []
                if "material_group" in params:
                    filters.append(
                        f"MaterialGroup eq '{self._sanitize_odata_value(str(params['material_group']))}'"
                    )

                query_params = {"$top": params.get("top", 100)}
                if filters:
                    query_params["$filter"] = " and ".join(filters)

                response = await client.get(
                    "/MaterialSet",
                    headers=headers,
                    params=query_params,
                )

            if response.status_code != 200:
                return ConnectorResult(
                    success=False,
                    error=f"SAP API error: {response.status_code}",
                )

            data = response.json()

            if "material_id" in params:
                # Single result
                material = self._map_material_master(data.get("d", {}))
                return ConnectorResult(
                    success=True,
                    data=material,
                    metadata={"tenant_id": str(tenant_id)},
                    classification="class_ii",
                )
            else:
                # Multiple results
                results = data.get("d", {}).get("results", [])
                materials = [
                    self._map_material_master(mat_data) for mat_data in results
                ]
                return ConnectorResult(
                    success=True,
                    data=materials,
                    metadata={
                        "count": len(materials),
                        "tenant_id": str(tenant_id),
                    },
                    classification="class_ii",
                )

        except Exception as exc:
            log.error("sap.get_material_master_failed", error=str(exc))
            return ConnectorResult(success=False, error=str(exc))

    # --------------------------------------------------------------------- #
    # Mapping functions: SAP OData responses -> normalized dataclasses
    # --------------------------------------------------------------------- #

    def _map_purchase_order(self, data: dict[str, Any]) -> PurchaseOrder:
        """Map SAP OData PurchaseOrder to normalized dataclass."""
        items_data = data.get("Items", {}).get("results", [])
        items = [self._map_purchase_order_item(item) for item in items_data]

        return PurchaseOrder(
            po_number=data.get("PONumber", ""),
            vendor_id=data.get("Vendor", ""),
            vendor_name=data.get("VendorName", ""),
            total_amount=float(data.get("TotalAmount", 0)),
            currency=data.get("Currency", "USD"),
            status=data.get("Status", ""),
            created_date=self._parse_sap_datetime(data.get("CreatedDate")),
            delivery_date=self._parse_sap_datetime(data.get("DeliveryDate")),
            items=items,
        )

    def _map_purchase_order_item(self, data: dict[str, Any]) -> PurchaseOrderItem:
        """Map SAP OData PurchaseOrderItem to normalized dataclass."""
        return PurchaseOrderItem(
            item_number=data.get("ItemNumber", ""),
            material_id=data.get("Material", ""),
            description=data.get("Description", ""),
            quantity=float(data.get("Quantity", 0)),
            unit_price=float(data.get("UnitPrice", 0)),
            total_price=float(data.get("TotalPrice", 0)),
        )

    def _map_inventory_item(self, data: dict[str, Any]) -> InventoryItem:
        """Map SAP OData MaterialStock to normalized dataclass."""
        return InventoryItem(
            material_id=data.get("Material", ""),
            description=data.get("Description", ""),
            plant_id=data.get("Plant", ""),
            storage_location=data.get("StorageLocation", ""),
            quantity_on_hand=float(data.get("Quantity", 0)),
            unit_of_measure=data.get("UnitOfMeasure", ""),
            valuation_class=data.get("ValuationClass", ""),
            last_updated=self._parse_sap_datetime(data.get("LastUpdated")),
        )

    def _map_cost_center(self, data: dict[str, Any]) -> CostCenter:
        """Map SAP OData CostCenter to normalized dataclass."""
        return CostCenter(
            cost_center_id=data.get("CostCenter", ""),
            name=data.get("Name", ""),
            department=data.get("Department", ""),
            responsible_person=data.get("ResponsiblePerson", ""),
            budget_allocated=float(data.get("BudgetAllocated", 0)),
            budget_consumed=float(data.get("BudgetConsumed", 0)),
            currency=data.get("Currency", "USD"),
        )

    def _map_material_master(self, data: dict[str, Any]) -> MaterialMaster:
        """Map SAP OData Material to normalized dataclass."""
        return MaterialMaster(
            material_id=data.get("Material", ""),
            description=data.get("Description", ""),
            material_type=data.get("MaterialType", ""),
            base_unit_of_measure=data.get("BaseUnitOfMeasure", ""),
            gross_weight=self._parse_float(data.get("GrossWeight")),
            net_weight=self._parse_float(data.get("NetWeight")),
            weight_unit=data.get("WeightUnit"),
            material_group=data.get("MaterialGroup", ""),
            procurement_type=data.get("ProcurementType", ""),
        )

    def _parse_sap_datetime(self, value: Any) -> datetime:
        """Parse SAP OData datetime format.

        SAP OData v2 uses: /Date(1234567890000)/
        Returns datetime object or current time if parsing fails.
        """
        if not value:
            return datetime.now(UTC)

        try:
            if isinstance(value, str) and value.startswith("/Date("):
                # Extract timestamp in milliseconds
                timestamp_ms = int(value.replace("/Date(", "").replace(")/", ""))
                return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
            return datetime.fromisoformat(str(value))
        except (ValueError, AttributeError):
            return datetime.now(UTC)

    def _parse_float(self, value: Any) -> float | None:
        """Safely parse float value."""
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
