"""Database replication management for multi-region deployments.

Manages PostgreSQL streaming replication configuration and provides
higher-level operations: lag monitoring, promotion, and tenant migration.

Design notes:
- Primary region has read-write access
- Replicas are read-only (streaming replication)
- Promotion is manual or triggered by FailoverManager
- Tenant migration is orchestrated at application layer via httpx calls
  to the platform API on each region
"""

from __future__ import annotations

import asyncio
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


class ReplicationRole(StrEnum):
    PRIMARY = "primary"
    REPLICA = "replica"
    STANDBY = "standby"


class ReplicationStatus(StrEnum):
    STREAMING = "streaming"
    CATCHUP = "catchup"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


@dataclass
class ReplicationState:
    region_name: str
    role: ReplicationRole
    status: ReplicationStatus
    lag_seconds: float | None
    primary_region: str | None
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))


# ------------------------------------------------------------------ #
# ReplicationManager
# ------------------------------------------------------------------ #


class ReplicationManager:
    """Manages database replication topology for multi-region platform.

    Tracks which region is primary and which are replicas, monitors
    replication lag, and orchestrates promotion during failover.
    """

    def __init__(self) -> None:
        self._primary_region: str | None = None
        self._replica_regions: list[str] = []
        self._replication_states: dict[str, ReplicationState] = {}
        self._region_endpoints: dict[str, str] = {}  # region_name -> API base URL
        self._api_key: str = ""

    # ---------------------------------------------------------------- #
    # Configuration
    # ---------------------------------------------------------------- #

    def configure_replication(
        self,
        primary_region: str,
        replica_regions: list[str],
        region_endpoints: dict[str, str] | None = None,
        api_key: str = "",
    ) -> None:
        """Set up the replication topology.

        Args:
            primary_region: Name of the primary (read-write) region
            replica_regions: Names of replica (read-only) regions
            region_endpoints: Map of region_name -> API base URL for management calls
            api_key: Bearer token for cross-region management API calls
        """
        self._primary_region = primary_region
        self._replica_regions = list(replica_regions)
        self._api_key = api_key

        if region_endpoints:
            self._region_endpoints.update(region_endpoints)

        # Initialise replication state tracking
        self._replication_states[primary_region] = ReplicationState(
            region_name=primary_region,
            role=ReplicationRole.PRIMARY,
            status=ReplicationStatus.STREAMING,
            lag_seconds=0.0,
            primary_region=None,
        )
        for replica in replica_regions:
            self._replication_states[replica] = ReplicationState(
                region_name=replica,
                role=ReplicationRole.REPLICA,
                status=ReplicationStatus.UNKNOWN,
                lag_seconds=None,
                primary_region=primary_region,
            )

        log.info(
            "replication.configured",
            primary=primary_region,
            replicas=replica_regions,
        )

    # ---------------------------------------------------------------- #
    # Lag monitoring
    # ---------------------------------------------------------------- #

    async def get_replication_lag(self, region_name: str) -> float:
        """Return replication lag in seconds for the given replica region.

        Calls the platform management API on the target region which
        exposes PostgreSQL's pg_last_xact_replay_timestamp() delta.

        Args:
            region_name: Target replica region name

        Returns:
            Lag in seconds (0.0 for primary, -1.0 on error)
        """
        state = self._replication_states.get(region_name)
        if not state:
            log.warning("replication.lag_check.unknown_region", region=region_name)
            return -1.0

        if state.role == ReplicationRole.PRIMARY:
            return 0.0

        endpoint = self._region_endpoints.get(region_name)
        if not endpoint:
            log.warning("replication.lag_check.no_endpoint", region=region_name)
            return -1.0

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{endpoint}/api/v1/infra/replication/lag",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
                lag = float(data.get("lag_seconds", -1.0))

                state.lag_seconds = lag
                state.status = ReplicationStatus.STREAMING if lag >= 0 else ReplicationStatus.STOPPED
                state.last_updated = datetime.now(UTC)

                log.debug(
                    "replication.lag_measured",
                    region=region_name,
                    lag_seconds=lag,
                )
                return lag

        except httpx.ConnectError as exc:
            log.error("replication.lag_check.connect_error", region=region_name, error=str(exc))
            state.status = ReplicationStatus.STOPPED
            state.lag_seconds = None
            return -1.0
        except Exception as exc:
            log.error("replication.lag_check.error", region=region_name, error=str(exc))
            return -1.0

    async def get_all_replication_lag(self) -> dict[str, float]:
        """Return lag for all replica regions concurrently."""
        tasks = {
            name: self.get_replication_lag(name)
            for name in self._replica_regions
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {
            name: (r if isinstance(r, float) else -1.0)
            for name, r in zip(tasks.keys(), results)
        }

    # ---------------------------------------------------------------- #
    # Promotion
    # ---------------------------------------------------------------- #

    async def promote_replica(self, region_name: str) -> dict[str, Any]:
        """Promote a replica region to primary.

        This triggers a controlled failover: the replica is instructed
        to become writable and the topology is updated here.

        Args:
            region_name: Region to promote to primary

        Returns:
            Promotion result with previous and new primary names
        """
        if region_name not in self._replica_regions:
            msg = f"Region '{region_name}' is not a known replica"
            log.error("replication.promote.not_replica", region=region_name)
            raise ValueError(msg)

        state = self._replication_states.get(region_name)
        if not state:
            raise ValueError(f"No replication state for region: {region_name}")

        endpoint = self._region_endpoints.get(region_name)
        previous_primary = self._primary_region

        log.warning(
            "replication.promote.starting",
            region=region_name,
            previous_primary=previous_primary,
        )

        if endpoint:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{endpoint}/api/v1/infra/replication/promote",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
                    resp.raise_for_status()
            except Exception as exc:
                log.error(
                    "replication.promote.api_call_failed",
                    region=region_name,
                    error=str(exc),
                )
                # Continue with local state update even if remote call fails
                # The caller (FailoverManager) handles the broader orchestration

        # Update local topology
        if previous_primary and previous_primary in self._replication_states:
            old_primary_state = self._replication_states[previous_primary]
            old_primary_state.role = ReplicationRole.REPLICA
            old_primary_state.primary_region = region_name
            old_primary_state.status = ReplicationStatus.STOPPED  # Was primary, now needs to restream

        state.role = ReplicationRole.PRIMARY
        state.primary_region = None
        state.lag_seconds = 0.0
        state.status = ReplicationStatus.STREAMING
        state.last_updated = datetime.now(UTC)

        self._primary_region = region_name
        self._replica_regions = [r for r in self._replica_regions if r != region_name]
        if previous_primary:
            self._replica_regions.append(previous_primary)

        log.info(
            "replication.promote.complete",
            new_primary=region_name,
            previous_primary=previous_primary,
        )

        return {
            "new_primary": region_name,
            "previous_primary": previous_primary,
            "replicas": self._replica_regions,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    # ---------------------------------------------------------------- #
    # Tenant migration
    # ---------------------------------------------------------------- #

    async def sync_tenant_data(
        self,
        tenant_id: str,
        source_region: str,
        target_region: str,
    ) -> dict[str, Any]:
        """Migrate tenant data from source to target region.

        Calls the export API on the source and import API on the target.
        This is an application-layer migration, not replication-layer.

        Args:
            tenant_id: Tenant to migrate
            source_region: Region holding current data
            target_region: Destination region

        Returns:
            Migration result with record counts and timing
        """
        source_endpoint = self._region_endpoints.get(source_region)
        target_endpoint = self._region_endpoints.get(target_region)

        if not source_endpoint:
            raise ValueError(f"No endpoint configured for source region: {source_region}")
        if not target_endpoint:
            raise ValueError(f"No endpoint configured for target region: {target_region}")

        log.info(
            "replication.tenant_sync.starting",
            tenant_id=tenant_id,
            source=source_region,
            target=target_region,
        )

        start_time = datetime.now(UTC)
        headers = {"Authorization": f"Bearer {self._api_key}"}

        async with httpx.AsyncClient(timeout=120.0) as client:
            # Step 1: Export from source
            export_resp = await client.post(
                f"{source_endpoint}/api/v1/admin/tenants/{tenant_id}/export",
                headers=headers,
            )
            export_resp.raise_for_status()
            export_data = export_resp.json()

            # Step 2: Import to target
            import_resp = await client.post(
                f"{target_endpoint}/api/v1/admin/tenants/{tenant_id}/import",
                json=export_data,
                headers=headers,
            )
            import_resp.raise_for_status()
            import_result = import_resp.json()

        elapsed = (datetime.now(UTC) - start_time).total_seconds()
        log.info(
            "replication.tenant_sync.complete",
            tenant_id=tenant_id,
            source=source_region,
            target=target_region,
            elapsed_seconds=elapsed,
        )

        return {
            "tenant_id": tenant_id,
            "source_region": source_region,
            "target_region": target_region,
            "records_migrated": import_result.get("records_imported", 0),
            "elapsed_seconds": elapsed,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    # ---------------------------------------------------------------- #
    # Introspection
    # ---------------------------------------------------------------- #

    def get_topology(self) -> dict[str, Any]:
        """Return current replication topology."""
        return {
            "primary_region": self._primary_region,
            "replica_regions": self._replica_regions,
            "states": {
                name: {
                    "role": state.role.value,
                    "status": state.status.value,
                    "lag_seconds": state.lag_seconds,
                    "primary_region": state.primary_region,
                    "last_updated": state.last_updated.isoformat(),
                }
                for name, state in self._replication_states.items()
            },
        }
