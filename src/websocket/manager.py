"""WebSocket ConnectionManager - tracks active connections per tenant/user.

Design:
- Singleton via get_connection_manager() - one instance per process.
- Thread-safe via asyncio (single-threaded event loop assumed).
- Three indexes maintained in parallel:
    1. _connections: WebSocket -> ConnectionMeta (primary reverse lookup)
    2. _by_user: (tenant_id, user_id) -> set[WebSocket]
    3. _by_conversation: conversation_id -> set[WebSocket]

All public methods are async to allow future Redis Pub/Sub fan-out without
changing the API surface (horizontal scaling path).

Usage:
    mgr = get_connection_manager()

    # When client connects
    await mgr.connect(websocket, tenant_id=tid, user_id=uid, conversation_id=cid)

    # Send a message to a specific user
    await mgr.send_to_user(tenant_id=tid, user_id=uid, message={"type": "status"})

    # Broadcast to all connections in a tenant
    await mgr.broadcast_to_tenant(tenant_id=tid, message={"type": "maintenance"})

    # When client disconnects
    await mgr.disconnect(websocket)
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

log = structlog.get_logger(__name__)


@dataclass
class _ConnectionMeta:
    """Metadata stored per active WebSocket connection."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID | None = None


class ConnectionManager:
    """Manages active WebSocket connections with tenant and user indexing.

    All mutations go through connect() / disconnect() which maintain three
    internal indexes for fast lookups:
    - _connections: primary store (WebSocket -> meta)
    - _by_user: (tenant_id, user_id) -> set of WebSockets
    - _by_tenant: tenant_id -> set of WebSockets
    - _by_conversation: conversation_id -> set of WebSockets
    """

    def __init__(self) -> None:
        self._connections: dict[Any, _ConnectionMeta] = {}
        self._by_user: dict[tuple[uuid.UUID, uuid.UUID], set[Any]] = {}
        self._by_tenant: dict[uuid.UUID, set[Any]] = {}
        self._by_conversation: dict[uuid.UUID, set[Any]] = {}

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    async def connect(
        self,
        websocket: WebSocket,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
    ) -> None:
        """Register a new WebSocket connection.

        Args:
            websocket: The active WebSocket connection.
            tenant_id: Tenant this connection belongs to.
            user_id: User who opened the connection.
            conversation_id: Optional conversation the socket is scoped to.
        """
        meta = _ConnectionMeta(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        self._connections[websocket] = meta

        # Index by (tenant_id, user_id)
        user_key = (tenant_id, user_id)
        self._by_user.setdefault(user_key, set()).add(websocket)

        # Index by tenant_id
        self._by_tenant.setdefault(tenant_id, set()).add(websocket)

        # Index by conversation_id
        if conversation_id is not None:
            self._by_conversation.setdefault(conversation_id, set()).add(websocket)

        log.info(
            "ws.connected",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            conversation_id=str(conversation_id) if conversation_id else None,
            total_connections=len(self._connections),
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection and clean up all indexes.

        Safe to call even if websocket is not registered (no-op).

        Args:
            websocket: The WebSocket to remove.
        """
        meta = self._connections.pop(websocket, None)
        if meta is None:
            return

        # Remove from user index
        user_key = (meta.tenant_id, meta.user_id)
        user_sockets = self._by_user.get(user_key)
        if user_sockets is not None:
            user_sockets.discard(websocket)
            if not user_sockets:
                del self._by_user[user_key]

        # Remove from tenant index
        tenant_sockets = self._by_tenant.get(meta.tenant_id)
        if tenant_sockets is not None:
            tenant_sockets.discard(websocket)
            if not tenant_sockets:
                del self._by_tenant[meta.tenant_id]

        # Remove from conversation index
        if meta.conversation_id is not None:
            conv_sockets = self._by_conversation.get(meta.conversation_id)
            if conv_sockets is not None:
                conv_sockets.discard(websocket)
                if not conv_sockets:
                    del self._by_conversation[meta.conversation_id]

        log.info(
            "ws.disconnected",
            tenant_id=str(meta.tenant_id),
            user_id=str(meta.user_id),
            total_connections=len(self._connections),
        )

    # ------------------------------------------------------------------ #
    # Targeted sends
    # ------------------------------------------------------------------ #

    async def send_to_user(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        message: dict[str, Any],
    ) -> None:
        """Send a JSON message to all connections belonging to a specific user.

        Args:
            tenant_id: Tenant scope (prevents cross-tenant leakage).
            user_id: Target user.
            message: JSON-serialisable dict to send.
        """
        sockets = self._by_user.get((tenant_id, user_id), set())
        await self._send_all(sockets, message)

    async def send_to_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        message: dict[str, Any],
    ) -> None:
        """Send a JSON message to all connections scoped to a conversation.

        Args:
            conversation_id: Target conversation.
            message: JSON-serialisable dict to send.
        """
        sockets = self._by_conversation.get(conversation_id, set())
        await self._send_all(sockets, message)

    async def broadcast_to_tenant(
        self,
        *,
        tenant_id: uuid.UUID,
        message: dict[str, Any],
    ) -> None:
        """Broadcast a JSON message to ALL connections within a tenant.

        Args:
            tenant_id: Target tenant.
            message: JSON-serialisable dict to send.
        """
        sockets = self._by_tenant.get(tenant_id, set())
        await self._send_all(sockets, message)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _send_all(
        self,
        sockets: set[Any],
        message: dict[str, Any],
    ) -> None:
        """Send message to a set of sockets, handling send failures gracefully.

        Dead connections are not removed here to avoid mutating the set
        during iteration; they will be cleaned up on the next disconnect().
        """
        if not sockets:
            return

        # Snapshot the set to avoid concurrent mutation issues
        targets = list(sockets)
        tasks = []
        for ws in targets:
            tasks.append(_safe_send_json(ws, message))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def connection_count(self) -> int:
        """Return the number of currently active connections."""
        return len(self._connections)


# ------------------------------------------------------------------ #
# Module-level singleton
# ------------------------------------------------------------------ #

_manager: ConnectionManager | None = None


def get_connection_manager() -> ConnectionManager:
    """Return the application-wide ConnectionManager singleton.

    Thread-safe in a single-threaded asyncio context. If you need true
    horizontal scaling, replace this with a Redis-backed implementation
    that implements the same interface.
    """
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager


# ------------------------------------------------------------------ #
# Safe send helper
# ------------------------------------------------------------------ #

async def _safe_send_json(websocket: WebSocket, message: dict[str, Any]) -> None:
    """Send JSON to a WebSocket, logging but not re-raising on failure."""
    try:
        await websocket.send_json(message)
    except Exception as exc:
        log.warning("ws.send_failed", error=str(exc))
