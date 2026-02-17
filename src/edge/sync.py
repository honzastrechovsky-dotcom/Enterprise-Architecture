"""Edge sync service.

Handles bidirectional synchronization between edge nodes and the
central server. Operates with offline-first semantics: all operations
are queued locally in SQLite and pushed when connectivity is available.

Conflict resolution policy: central wins by default.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import structlog
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

log = structlog.get_logger(__name__)


# ------------------------------------------------------------------ #
# Enums and models
# ------------------------------------------------------------------ #


class SyncDirection(StrEnum):
    PUSH = "push"
    PULL = "pull"


class SyncItemStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    SYNCED = "synced"
    FAILED = "failed"
    CONFLICT = "conflict"


class SyncItem(BaseModel):
    id: str
    item_type: str
    item_id: str
    data: dict[str, Any]
    direction: SyncDirection
    status: SyncItemStatus
    created_at: datetime
    updated_at: datetime
    retry_count: int
    error_message: str | None


class SyncStatus(BaseModel):
    last_sync_push: datetime | None
    last_sync_pull: datetime | None
    pending_push: int
    pending_pull: int
    failed_items: int
    is_connected: bool
    sync_endpoint: str
    node_id: str


# ------------------------------------------------------------------ #
# EdgeSyncService
# ------------------------------------------------------------------ #


class EdgeSyncService:
    """Manages sync queue and bidirectional data sync with central server.

    All data is persisted to a local SQLite database so items survive
    restarts and can be retried after connectivity is restored.
    """

    def __init__(
        self,
        db_url: str = "sqlite+aiosqlite:////data/sync_queue.db",
        node_id: str = "edge-node-001",
        max_retry: int = 5,
        retry_backoff_seconds: int = 60,
        batch_size: int = 100,
    ) -> None:
        self._db_url = db_url
        self._node_id = node_id
        self._max_retry = max_retry
        self._retry_backoff = retry_backoff_seconds
        self._batch_size = batch_size
        self._engine: Any | None = None
        self._session_factory: async_sessionmaker | None = None
        self._last_sync_push: datetime | None = None
        self._last_sync_pull: datetime | None = None
        self._is_initialized = False

    # ---------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------- #

    async def initialize(self) -> None:
        """Create database engine and schema."""
        if self._is_initialized:
            return
        self._engine = create_async_engine(self._db_url, echo=False)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )
        await self._create_schema()
        self._is_initialized = True
        log.info("edge_sync.initialized", node_id=self._node_id, db_url=self._db_url)

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._is_initialized = False

    async def _create_schema(self) -> None:
        assert self._engine is not None
        async with self._engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id TEXT PRIMARY KEY,
                    item_type TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'push',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT
                )
            """))
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_sync_queue_status
                ON sync_queue(status, direction, created_at)
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """))

    # ---------------------------------------------------------------- #
    # Queue management
    # ---------------------------------------------------------------- #

    async def queue_for_sync(
        self,
        item_type: str,
        item_id: str,
        data: dict[str, Any],
        direction: SyncDirection = SyncDirection.PUSH,
    ) -> str:
        """Queue an item for the next sync cycle.

        Args:
            item_type: Entity type (e.g., "conversation", "document")
            item_id: Entity identifier
            data: Payload to sync
            direction: PUSH (edge->central) or PULL request marker

        Returns:
            Queue entry ID
        """
        await self._ensure_initialized()
        now = datetime.now(UTC).isoformat()
        entry_id = str(uuid.uuid4())

        async with self._session_factory() as session:  # type: ignore[misc]
            await session.execute(
                text("""
                    INSERT INTO sync_queue
                        (id, item_type, item_id, data, direction,
                         status, created_at, updated_at, retry_count)
                    VALUES
                        (:id, :item_type, :item_id, :data, :direction,
                         'pending', :now, :now, 0)
                    ON CONFLICT(id) DO NOTHING
                """),
                {
                    "id": entry_id,
                    "item_type": item_type,
                    "item_id": item_id,
                    "data": json.dumps(data),
                    "direction": direction.value,
                    "now": now,
                },
            )
            await session.commit()

        log.debug(
            "edge_sync.queued",
            entry_id=entry_id,
            item_type=item_type,
            item_id=item_id,
            direction=direction,
        )
        return entry_id

    async def _get_pending_items(
        self,
        direction: SyncDirection,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        await self._ensure_initialized()
        batch = limit or self._batch_size
        async with self._session_factory() as session:  # type: ignore[misc]
            result = await session.execute(
                text("""
                    SELECT id, item_type, item_id, data, direction,
                           status, created_at, updated_at, retry_count, error_message
                    FROM sync_queue
                    WHERE status IN ('pending', 'failed')
                      AND direction = :direction
                      AND retry_count < :max_retry
                    ORDER BY created_at ASC
                    LIMIT :limit
                """),
                {
                    "direction": direction.value,
                    "max_retry": self._max_retry,
                    "limit": batch,
                },
            )
            return [dict(row._mapping) for row in result]

    async def _update_item_status(
        self,
        entry_id: str,
        status: SyncItemStatus,
        error_message: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._session_factory() as session:  # type: ignore[misc]
            await session.execute(
                text("""
                    UPDATE sync_queue
                    SET status = :status,
                        updated_at = :now,
                        retry_count = retry_count + 1,
                        error_message = :error
                    WHERE id = :id
                """),
                {
                    "id": entry_id,
                    "status": status.value,
                    "now": now,
                    "error": error_message,
                },
            )
            await session.commit()

    # ---------------------------------------------------------------- #
    # Sync operations
    # ---------------------------------------------------------------- #

    async def sync_to_central(
        self,
        endpoint: str,
        api_key: str,
    ) -> dict[str, Any]:
        """Push pending local data to the central server.

        Handles offline gracefully: returns summary with failed items
        queued for retry. Conflict resolution: central wins.

        Args:
            endpoint: Central server sync URL
            api_key: Bearer token for central API

        Returns:
            Summary dict with synced, failed, and skipped counts
        """
        await self._ensure_initialized()
        pending = await self._get_pending_items(SyncDirection.PUSH)
        if not pending:
            log.debug("edge_sync.push.nothing_pending")
            return {"synced": 0, "failed": 0, "skipped": 0}

        synced = failed = skipped = 0
        headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Edge-Node-Id": self._node_id,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                payload = {
                    "node_id": self._node_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "items": [
                        {
                            "id": item["id"],
                            "item_type": item["item_type"],
                            "item_id": item["item_id"],
                            "data": json.loads(item["data"]),
                        }
                        for item in pending
                    ],
                }
                response = await client.post(
                    f"{endpoint}/push",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                result = response.json()

                # Process per-item results
                item_results: dict[str, str] = result.get("results", {})
                for item in pending:
                    item_result = item_results.get(item["id"], "synced")
                    if item_result == "conflict":
                        # Central wins: mark as conflict and skip
                        await self._update_item_status(
                            item["id"],
                            SyncItemStatus.CONFLICT,
                            "Central version wins per conflict resolution policy",
                        )
                        skipped += 1
                    else:
                        await self._update_item_status(item["id"], SyncItemStatus.SYNCED)
                        synced += 1

        except httpx.ConnectError as exc:
            log.warning("edge_sync.push.offline", error=str(exc))
            for item in pending:
                await self._update_item_status(
                    item["id"],
                    SyncItemStatus.FAILED,
                    f"Connection failed: {exc}",
                )
            failed = len(pending)
        except httpx.HTTPStatusError as exc:
            log.error(
                "edge_sync.push.http_error",
                status_code=exc.response.status_code,
                error=str(exc),
            )
            for item in pending:
                await self._update_item_status(
                    item["id"],
                    SyncItemStatus.FAILED,
                    f"HTTP {exc.response.status_code}: {exc}",
                )
            failed = len(pending)
        except Exception as exc:
            log.error("edge_sync.push.unexpected_error", error=str(exc))
            for item in pending:
                await self._update_item_status(
                    item["id"],
                    SyncItemStatus.FAILED,
                    str(exc),
                )
            failed = len(pending)
        else:
            self._last_sync_push = datetime.now(UTC)
            await self._save_state("last_sync_push", self._last_sync_push.isoformat())

        log.info(
            "edge_sync.push.complete",
            synced=synced,
            failed=failed,
            skipped=skipped,
        )
        return {"synced": synced, "failed": failed, "skipped": skipped}

    async def sync_from_central(
        self,
        endpoint: str,
        api_key: str,
    ) -> dict[str, Any]:
        """Pull updates from the central server.

        Fetches data changes since the last pull. Central data always
        wins in conflict scenarios.

        Args:
            endpoint: Central server sync URL
            api_key: Bearer token for central API

        Returns:
            Summary dict with pulled item count
        """
        await self._ensure_initialized()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Edge-Node-Id": self._node_id,
            "Content-Type": "application/json",
        }

        since = None
        if self._last_sync_pull:
            since = self._last_sync_pull.isoformat()
        else:
            since = await self._load_state("last_sync_pull")

        pulled = 0
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                params: dict[str, str] = {"node_id": self._node_id}
                if since:
                    params["since"] = since

                response = await client.get(
                    f"{endpoint}/pull",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                result = response.json()
                items = result.get("items", [])
                pulled = len(items)

                # Store pulled items locally as conflict-resolved entries
                for item in items:
                    await self.queue_for_sync(
                        item_type=item["item_type"],
                        item_id=item["item_id"],
                        data=item["data"],
                        direction=SyncDirection.PULL,
                    )

        except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
            log.warning("edge_sync.pull.failed", error=str(exc))
            return {"pulled": 0, "error": str(exc)}
        except Exception as exc:
            log.error("edge_sync.pull.unexpected_error", error=str(exc))
            return {"pulled": 0, "error": str(exc)}
        else:
            self._last_sync_pull = datetime.now(UTC)
            await self._save_state("last_sync_pull", self._last_sync_pull.isoformat())

        log.info("edge_sync.pull.complete", pulled=pulled)
        return {"pulled": pulled}

    # ---------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------- #

    async def get_sync_status(self) -> SyncStatus:
        """Return current sync status including pending counts and connectivity."""
        await self._ensure_initialized()

        pending_push = 0
        pending_pull = 0
        failed_items = 0

        async with self._session_factory() as session:  # type: ignore[misc]
            result = await session.execute(
                text("""
                    SELECT direction, status, COUNT(*) as cnt
                    FROM sync_queue
                    WHERE status IN ('pending', 'failed')
                    GROUP BY direction, status
                """)
            )
            for row in result:
                if row.status == "failed":
                    failed_items += row.cnt
                elif row.direction == "push":
                    pending_push += row.cnt
                else:
                    pending_pull += row.cnt

        # Load persisted timestamps if not in memory
        if not self._last_sync_push:
            val = await self._load_state("last_sync_push")
            if val:
                self._last_sync_push = datetime.fromisoformat(val)
        if not self._last_sync_pull:
            val = await self._load_state("last_sync_pull")
            if val:
                self._last_sync_pull = datetime.fromisoformat(val)

        sync_endpoint = await self._load_state("sync_endpoint") or "unknown"

        return SyncStatus(
            last_sync_push=self._last_sync_push,
            last_sync_pull=self._last_sync_pull,
            pending_push=pending_push,
            pending_pull=pending_pull,
            failed_items=failed_items,
            is_connected=await self._check_connectivity(),
            sync_endpoint=sync_endpoint,
            node_id=self._node_id,
        )

    # ---------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------- #

    async def _ensure_initialized(self) -> None:
        if not self._is_initialized:
            await self.initialize()

    async def _check_connectivity(self) -> bool:
        """Quick TCP-level connectivity probe."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                state = await self._load_state("sync_endpoint")
                if not state:
                    return False
                await client.head(state)
                return True
        except Exception:
            return False

    async def _save_state(self, key: str, value: str) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._session_factory() as session:  # type: ignore[misc]
            await session.execute(
                text("""
                    INSERT INTO sync_state (key, value, updated_at)
                    VALUES (:key, :value, :now)
                    ON CONFLICT(key) DO UPDATE
                    SET value = excluded.value, updated_at = excluded.updated_at
                """),
                {"key": key, "value": value, "now": now},
            )
            await session.commit()

    async def _load_state(self, key: str) -> str | None:
        async with self._session_factory() as session:  # type: ignore[misc]
            result = await session.execute(
                text("SELECT value FROM sync_state WHERE key = :key"),
                {"key": key},
            )
            row = result.fetchone()
            return row.value if row else None
