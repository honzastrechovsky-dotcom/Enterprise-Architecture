"""Multi-region failover management.

Detects region failures and orchestrates failover to healthy replicas.
Supports both automatic (threshold-based) and manual (admin-triggered)
failover. State is tracked in-memory; for production use, integrate
with a distributed lock (e.g., etcd, Redis Sentinel).

Failover sequence:
1. detect_failure() probes the region
2. trigger_failover() promotes replica and updates routing
3. get_failover_status() tracks all active/historical failovers
4. rollback_failover() returns to original primary when resolved
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import structlog

from src.multiregion.replication import ReplicationManager
from src.multiregion.routing import RegionRouter, RegionStatus

log = structlog.get_logger(__name__)


# ------------------------------------------------------------------ #
# Types
# ------------------------------------------------------------------ #


class FailoverState(StrEnum):
    NONE = "none"
    DETECTING = "detecting"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


@dataclass
class FailoverRecord:
    id: str
    failed_region: str
    target_region: str
    state: FailoverState
    initiated_by: str  # "auto" or "manual:<user>"
    started_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ #
# FailoverManager
# ------------------------------------------------------------------ #


class FailoverManager:
    """Orchestrates detection and execution of region failovers.

    Integrates with RegionRouter to update routing and
    ReplicationManager to promote a replica to primary.
    """

    def __init__(
        self,
        router: RegionRouter,
        replication: ReplicationManager,
        health_check_timeout_seconds: float = 5.0,
        failure_threshold: int = 3,
        failure_check_interval_seconds: float = 10.0,
    ) -> None:
        self._router = router
        self._replication = replication
        self._health_timeout = health_check_timeout_seconds
        self._failure_threshold = failure_threshold
        self._check_interval = failure_check_interval_seconds
        self._failover_records: dict[str, FailoverRecord] = {}
        self._active_failover: str | None = None  # region name under active failover
        self._consecutive_failures: dict[str, int] = {}
        self._original_primary: str | None = None

    # ---------------------------------------------------------------- #
    # Failure detection
    # ---------------------------------------------------------------- #

    async def detect_failure(self, region_name: str) -> bool:
        """Check if a region is failing.

        Makes repeated health probes up to `failure_threshold` times.
        A region is considered failed only after all probes fail,
        reducing false positives from transient network blips.

        Args:
            region_name: Region to probe

        Returns:
            True if the region is confirmed failed
        """
        region = self._router.get_region(region_name)
        if not region:
            log.warning("failover.detect.unknown_region", region=region_name)
            return False

        log.info("failover.detect.probing", region=region_name)

        consecutive = 0
        for attempt in range(self._failure_threshold):
            try:
                async with httpx.AsyncClient(timeout=self._health_timeout) as client:
                    resp = await client.get(f"{region.endpoint}/health/live")
                    if resp.status_code == 200:
                        # Recovery — reset counter
                        self._consecutive_failures[region_name] = 0
                        log.info(
                            "failover.detect.probe_ok",
                            region=region_name,
                            attempt=attempt + 1,
                        )
                        return False
                    consecutive += 1
            except Exception as exc:
                consecutive += 1
                log.warning(
                    "failover.detect.probe_failed",
                    region=region_name,
                    attempt=attempt + 1,
                    error=str(exc),
                )

            if attempt < self._failure_threshold - 1:
                await asyncio.sleep(self._check_interval)

        # All probes failed
        self._consecutive_failures[region_name] = consecutive
        region.status = RegionStatus.UNAVAILABLE
        log.error(
            "failover.detect.region_failed",
            region=region_name,
            consecutive_failures=consecutive,
        )
        return True

    # ---------------------------------------------------------------- #
    # Failover execution
    # ---------------------------------------------------------------- #

    async def trigger_failover(
        self,
        failed_region: str,
        target_region: str,
        initiated_by: str = "manual",
    ) -> FailoverRecord:
        """Orchestrate failover from failed_region to target_region.

        Steps:
        1. Validate no active failover is in progress
        2. Mark failed region as unavailable in router
        3. Promote target replica via ReplicationManager
        4. Update routing to direct traffic to target
        5. Record failover for audit

        Args:
            failed_region: Region that has failed
            target_region: Replica region to promote
            initiated_by: "auto" or "manual:<username>"

        Returns:
            FailoverRecord with current state
        """
        if self._active_failover:
            raise RuntimeError(
                f"Failover already in progress for region: {self._active_failover}"
            )

        import uuid
        record_id = str(uuid.uuid4())
        record = FailoverRecord(
            id=record_id,
            failed_region=failed_region,
            target_region=target_region,
            state=FailoverState.IN_PROGRESS,
            initiated_by=initiated_by,
            started_at=datetime.now(UTC),
        )
        self._failover_records[record_id] = record
        self._active_failover = failed_region

        log.warning(
            "failover.trigger.starting",
            record_id=record_id,
            failed_region=failed_region,
            target_region=target_region,
            initiated_by=initiated_by,
        )

        try:
            # Step 1: Mark failed region as unavailable
            failed = self._router.get_region(failed_region)
            if failed:
                failed.status = RegionStatus.UNAVAILABLE
                self._original_primary = failed_region if failed.is_primary else self._original_primary

            # Step 2: Promote replica
            promotion_result = await self._replication.promote_replica(target_region)

            # Step 3: Update router - mark target as primary
            target = self._router.get_region(target_region)
            if target:
                target.is_primary = True
                target.status = RegionStatus.FAILOVER

            # Step 4: Demote old primary in router
            if failed:
                failed.is_primary = False

            record.state = FailoverState.COMPLETE
            record.completed_at = datetime.now(UTC)
            record.metadata["promotion_result"] = promotion_result

            log.info(
                "failover.trigger.complete",
                record_id=record_id,
                new_primary=target_region,
                elapsed_seconds=(
                    record.completed_at - record.started_at
                ).total_seconds(),
            )

        except Exception as exc:
            record.state = FailoverState.FAILED
            record.error_message = str(exc)
            record.completed_at = datetime.now(UTC)
            log.error(
                "failover.trigger.failed",
                record_id=record_id,
                error=str(exc),
            )
            raise
        finally:
            self._active_failover = None

        return record

    # ---------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------- #

    def get_failover_status(self) -> dict[str, Any]:
        """Return current failover state and history.

        Returns:
            Dict with active failover info and recent history
        """
        active = None
        if self._active_failover:
            # Find the in-progress record
            for record in reversed(list(self._failover_records.values())):
                if (
                    record.failed_region == self._active_failover
                    and record.state == FailoverState.IN_PROGRESS
                ):
                    active = _record_to_dict(record)
                    break

        history = [
            _record_to_dict(r)
            for r in sorted(
                self._failover_records.values(),
                key=lambda r: r.started_at,
                reverse=True,
            )
        ]

        return {
            "active_failover": active,
            "is_failover_active": self._active_failover is not None,
            "original_primary": self._original_primary,
            "consecutive_failures": dict(self._consecutive_failures),
            "history": history,
            "total_failovers": len(self._failover_records),
        }

    # ---------------------------------------------------------------- #
    # Rollback
    # ---------------------------------------------------------------- #

    async def rollback_failover(self, region_name: str) -> dict[str, Any]:
        """Return to the original primary region after recovery.

        Promotes region_name back to primary and marks the previous
        failover target as replica. Only valid when no active failover
        is in progress.

        Args:
            region_name: Region to restore as primary

        Returns:
            Rollback result dict
        """
        if self._active_failover:
            raise RuntimeError("Cannot rollback while a failover is in progress")

        region = self._router.get_region(region_name)
        if not region:
            raise ValueError(f"Unknown region: {region_name}")

        if region.status == RegionStatus.UNAVAILABLE:
            raise ValueError(
                f"Region '{region_name}' is still unavailable. "
                "Verify it is healthy before rollback."
            )

        log.warning(
            "failover.rollback.starting",
            region=region_name,
            original_primary=self._original_primary,
        )

        # Promote original primary back
        result = await self._replication.promote_replica(region_name)

        # Update router
        region.is_primary = True
        region.status = RegionStatus.HEALTHY

        # Demote current (failover) primary
        for r in self._router.list_regions():
            if r.name != region_name and r.is_primary:
                r.is_primary = False
                r.status = RegionStatus.HEALTHY

        # Reset consecutive failure tracking
        self._consecutive_failures.pop(region_name, None)
        self._original_primary = None

        log.info("failover.rollback.complete", region=region_name)

        return {
            "rolled_back_to": region_name,
            "promotion_result": result,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    # ---------------------------------------------------------------- #
    # Auto-failover loop (optional background task)
    # ---------------------------------------------------------------- #

    async def run_auto_detection(
        self,
        regions_to_watch: list[str],
        preferred_failover: dict[str, str],
    ) -> None:
        """Background task: continuously monitor regions and auto-failover.

        Args:
            regions_to_watch: Region names to probe
            preferred_failover: Map of region_name -> preferred failover target
        """
        log.info("failover.auto_detection.started", regions=regions_to_watch)
        while True:
            await asyncio.sleep(self._check_interval)
            for region_name in regions_to_watch:
                if self._active_failover:
                    break
                try:
                    failed = await self.detect_failure(region_name)
                    if failed:
                        target = preferred_failover.get(region_name)
                        if target:
                            log.warning(
                                "failover.auto_detection.triggering",
                                failed=region_name,
                                target=target,
                            )
                            await self.trigger_failover(
                                region_name, target, initiated_by="auto"
                            )
                except Exception as exc:
                    log.error(
                        "failover.auto_detection.error",
                        region=region_name,
                        error=str(exc),
                    )


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _record_to_dict(record: FailoverRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "failed_region": record.failed_region,
        "target_region": record.target_region,
        "state": record.state.value,
        "initiated_by": record.initiated_by,
        "started_at": record.started_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "error_message": record.error_message,
        "metadata": record.metadata,
    }
