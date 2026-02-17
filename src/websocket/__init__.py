"""WebSocket package for real-time agent communication.

Provides:
- ConnectionManager: tracks active WebSocket connections per tenant/user
- authenticate_websocket: validates JWT from query param or first message
- AgentEventEmitter: emits agent lifecycle events to connected clients
- ws_router: FastAPI router with /api/v1/ws/chat endpoint
"""

from src.websocket.events import AgentEventEmitter
from src.websocket.manager import ConnectionManager, get_connection_manager

__all__ = [
    "ConnectionManager",
    "get_connection_manager",
    "AgentEventEmitter",
]
