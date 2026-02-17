"""Multi-region request routing.

Routes requests to the optimal region based on:
1. Data residency rules (tenant-to-region affinity)
2. Region health status
3. Latency measurements (when available)

The primary safety check is data residency: a tenant's data must
never be processed in a region where it is not permitted.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


# ------------------------------------------------------------------ #
# Types
# ------------------------------------------------------------------ #


class RegionStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    FAILOVER = "failover"


@dataclass
class RegionConfig:
    """Configuration for a single region."""

    name: str
    endpoint: str
    is_primary: bool
    allowed_tenants: list[str] = field(default_factory=list)  # empty = all tenants
    excluded_tenants: list[str] = field(default_factory=list)
    status: RegionStatus = RegionStatus.HEALTHY
    latency_ms: float | None = None
    last_health_check: datetime | None = None

    @property
    def accepts_all_tenants(self) -> bool:
        return not self.allowed_tenants

    def allows_tenant(self, tenant_id: str) -> bool:
        if tenant_id in self.excluded_tenants:
            return False
        if self.allowed_tenants:
            return tenant_id in self.allowed_tenants
        return True


@dataclass
class RoutingDecision:
    region_name: str
    endpoint: str
    reason: str
    latency_ms: float | None = None
    is_failover: bool = False


# ------------------------------------------------------------------ #
# RegionRouter
# ------------------------------------------------------------------ #


class RegionRouter:
    """Routes requests to the optimal region for a given tenant.

    Data residency is always checked first. If a tenant's data residency
    rules exclude a region, requests are never routed there regardless
    of latency or health.
    """

    def __init__(self, regions: list[RegionConfig] | None = None) -> None:
        self._regions: dict[str, RegionConfig] = {}
        self._tenant_residency: dict[str, str] = {}  # tenant_id -> preferred region

        if regions:
            for region in regions:
                self._regions[region.name] = region

    # ---------------------------------------------------------------- #
    # Region management
    # ---------------------------------------------------------------- #

    def register_region(self, region: RegionConfig) -> None:
        """Register or update a region configuration."""
        self._regions[region.name] = region
        log.info("region_router.registered", region=region.name, primary=region.is_primary)

    def set_tenant_residency(self, tenant_id: str, region_name: str) -> None:
        """Pin a tenant to a specific region for data residency."""
        if region_name not in self._regions:
            raise ValueError(f"Unknown region: {region_name}")
        self._tenant_residency[tenant_id] = region_name
        log.info(
            "region_router.residency_set",
            tenant_id=tenant_id,
            region=region_name,
        )

    # ---------------------------------------------------------------- #
    # Routing
    # ---------------------------------------------------------------- #

    def get_optimal_region(self, tenant_id: str) -> RegionConfig | None:
        """Return the best region for this tenant.

        Selection priority:
        1. Tenant pinned residency region (if healthy)
        2. Primary region (if allowed for tenant and healthy)
        3. Healthy secondary with lowest latency
        4. Any healthy region allowing this tenant (failover)

        Args:
            tenant_id: Tenant UUID string

        Returns:
            Best available RegionConfig, or None if no region available
        """
        healthy_regions = [
            r for r in self._regions.values()
            if r.status in (RegionStatus.HEALTHY, RegionStatus.DEGRADED)
            and r.allows_tenant(tenant_id)
        ]

        if not healthy_regions:
            log.warning("region_router.no_region_available", tenant_id=tenant_id)
            return None

        # Priority 1: pinned residency
        pinned = self._tenant_residency.get(tenant_id)
        if pinned and pinned in self._regions:
            region = self._regions[pinned]
            if region.status != RegionStatus.UNAVAILABLE and region.allows_tenant(tenant_id):
                log.debug(
                    "region_router.selected_pinned",
                    tenant_id=tenant_id,
                    region=pinned,
                )
                return region

        # Priority 2: primary region
        primaries = [r for r in healthy_regions if r.is_primary]
        if primaries:
            best_primary = min(
                primaries,
                key=lambda r: r.latency_ms if r.latency_ms is not None else float("inf"),
            )
            log.debug(
                "region_router.selected_primary",
                tenant_id=tenant_id,
                region=best_primary.name,
            )
            return best_primary

        # Priority 3: lowest latency secondary
        best = min(
            healthy_regions,
            key=lambda r: r.latency_ms if r.latency_ms is not None else float("inf"),
        )
        log.debug(
            "region_router.selected_secondary",
            tenant_id=tenant_id,
            region=best.name,
        )
        return best

    def route_request(
        self,
        tenant_id: str,
        regions: list[RegionConfig] | None = None,
    ) -> RoutingDecision | None:
        """Produce a routing decision for the given tenant.

        Args:
            tenant_id: Tenant UUID string
            regions: Override region list (uses registered regions if None)

        Returns:
            RoutingDecision with target region and reasoning, or None
        """
        if regions is not None:
            # Temporary context with provided regions
            temp_router = RegionRouter(regions)
            temp_router._tenant_residency = dict(self._tenant_residency)
            region = temp_router.get_optimal_region(tenant_id)
        else:
            region = self.get_optimal_region(tenant_id)

        if not region:
            return None

        pinned = self._tenant_residency.get(tenant_id)
        is_failover = bool(pinned and region.name != pinned)

        reason_parts = []
        if pinned == region.name:
            reason_parts.append("data residency pin")
        elif region.is_primary:
            reason_parts.append("primary region")
        else:
            reason_parts.append("failover to healthy secondary")
        if region.latency_ms is not None:
            reason_parts.append(f"latency={region.latency_ms:.0f}ms")

        return RoutingDecision(
            region_name=region.name,
            endpoint=region.endpoint,
            reason=", ".join(reason_parts),
            latency_ms=region.latency_ms,
            is_failover=is_failover,
        )

    def check_data_residency(
        self,
        tenant_id: str,
        target_region: str,
    ) -> bool:
        """Verify that a tenant's data can be processed in the target region.

        Args:
            tenant_id: Tenant UUID string
            target_region: Region name to check

        Returns:
            True if the tenant is permitted in this region
        """
        region = self._regions.get(target_region)
        if not region:
            log.warning(
                "region_router.residency_check.unknown_region",
                tenant_id=tenant_id,
                target_region=target_region,
            )
            return False

        allowed = region.allows_tenant(tenant_id)
        log.debug(
            "region_router.residency_check",
            tenant_id=tenant_id,
            target_region=target_region,
            allowed=allowed,
        )
        return allowed

    async def get_region_health(self) -> dict[str, dict[str, Any]]:
        """Probe all regions and return health status map.

        Makes a concurrent HEAD request to each region's /health/live
        endpoint and records latency.

        Returns:
            Dict mapping region_name -> health dict
        """
        async def _probe(region: RegionConfig) -> tuple[str, dict[str, Any]]:
            start = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{region.endpoint}/health/live")
                    latency_ms = (time.monotonic() - start) * 1000
                    region.latency_ms = latency_ms
                    region.last_health_check = datetime.now(UTC)

                    if resp.status_code == 200:
                        region.status = RegionStatus.HEALTHY
                        healthy = True
                    else:
                        region.status = RegionStatus.DEGRADED
                        healthy = False

                    return region.name, {
                        "status": region.status.value,
                        "healthy": healthy,
                        "latency_ms": round(latency_ms, 1),
                        "last_check": region.last_health_check.isoformat(),
                        "is_primary": region.is_primary,
                        "endpoint": region.endpoint,
                    }
            except Exception as exc:
                region.status = RegionStatus.UNAVAILABLE
                region.latency_ms = None
                region.last_health_check = datetime.now(UTC)
                return region.name, {
                    "status": RegionStatus.UNAVAILABLE.value,
                    "healthy": False,
                    "latency_ms": None,
                    "last_check": region.last_health_check.isoformat(),
                    "is_primary": region.is_primary,
                    "endpoint": region.endpoint,
                    "error": str(exc),
                }

        results = await asyncio.gather(*[_probe(r) for r in self._regions.values()])
        return dict(results)

    # ---------------------------------------------------------------- #
    # Introspection
    # ---------------------------------------------------------------- #

    def list_regions(self) -> list[RegionConfig]:
        return list(self._regions.values())

    def get_region(self, name: str) -> RegionConfig | None:
        return self._regions.get(name)
