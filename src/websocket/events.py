"""AgentEventEmitter - emits agent lifecycle events to WebSocket clients.

The emitter is a thin adapter between the agent runtime and the
ConnectionManager. It translates agent lifecycle events into the
wire-format messages sent to the client over WebSocket.

Event mapping:
    emit_agent_started()  -> {"type": "status", "status": "thinking"}
    emit_thinking()       -> {"type": "status", "status": "thinking"}
    emit_tool_call()      -> {"type": "status", "status": "searching"}
    emit_tool_result()    -> {"type": "status", "status": "generating"}
    emit_generating()     -> {"type": "status", "status": "generating"}
    emit_response_chunk() -> {"type": "response", "content": "...", "done": false}
    emit_agent_completed()-> {"type": "response", "content": "...", "done": true}
    emit_error()          -> {"type": "error", "message": "..."}

The emitter is intentionally lightweight. It does not hold any
conversation state; that lives in the AgentRuntime.

Usage:
    emitter = AgentEventEmitter(
        manager=get_connection_manager(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        conversation_id=conversation.id,
    )

    await emitter.emit_thinking("Retrieving relevant context...")
    await emitter.emit_tool_call("rag_retrieval", {"query": "..."})
    await emitter.emit_generating()
    await emitter.emit_response_chunk("Hello, ")
    await emitter.emit_response_chunk("world.")
    await emitter.emit_agent_completed("Hello, world.", citations=[...])
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from src.websocket.manager import ConnectionManager

log = structlog.get_logger(__name__)


class AgentEventEmitter:
    """Emits structured WebSocket events for agent lifecycle transitions.

    All emit_* methods are fire-and-forget from the agent's perspective;
    WebSocket send failures are logged but not propagated back to the agent.
    """

    def __init__(
        self,
        *,
        manager: ConnectionManager,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
    ) -> None:
        """
        Args:
            manager: The ConnectionManager to route messages through.
            tenant_id: Tenant scope for the connection lookup.
            user_id: User whose connection(s) receive events.
            conversation_id: Optional conversation scope.
        """
        self._manager = manager
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._conversation_id = conversation_id

    # ------------------------------------------------------------------ #
    # Status events
    # ------------------------------------------------------------------ #

    async def emit_agent_started(self) -> None:
        """Notify client that the agent has started processing."""
        await self._send_status("thinking")

    async def emit_thinking(self, detail: str = "") -> None:
        """Notify client that the agent is reasoning/thinking.

        Args:
            detail: Optional human-readable description of current thought.
        """
        msg: dict[str, Any] = {"type": "status", "status": "thinking"}
        if detail:
            msg["detail"] = detail
        await self._send(msg)

    async def emit_tool_call(self, tool_name: str, args: dict[str, Any]) -> None:
        """Notify client that the agent is calling a tool (e.g. RAG retrieval).

        Args:
            tool_name: Name of the tool being called.
            args: Tool call arguments (sanitised, no secrets).
        """
        await self._send({
            "type": "status",
            "status": "searching",
            "tool": tool_name,
        })

    async def emit_tool_result(self, tool_name: str, result_summary: str = "") -> None:
        """Notify client that a tool call completed.

        Args:
            tool_name: Name of the tool that finished.
            result_summary: Short summary of the result (optional).
        """
        msg: dict[str, Any] = {
            "type": "status",
            "status": "generating",
            "tool": tool_name,
        }
        if result_summary:
            msg["summary"] = result_summary
        await self._send(msg)

    async def emit_generating(self) -> None:
        """Notify client that the agent is generating its response."""
        await self._send_status("generating")

    # ------------------------------------------------------------------ #
    # Response events
    # ------------------------------------------------------------------ #

    async def emit_response_chunk(self, content: str) -> None:
        """Stream a partial response token to the client.

        Args:
            content: Partial response text (single token or small chunk).
        """
        await self._send({
            "type": "response",
            "content": content,
            "done": False,
        })

    async def emit_agent_completed(
        self,
        full_response: str,
        *,
        citations: list[dict[str, Any]] | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> None:
        """Send the final response message marking the turn as complete.

        Args:
            full_response: Complete assembled response text.
            citations: Optional list of citation dicts.
            conversation_id: Conversation ID to include in the message.
        """
        msg: dict[str, Any] = {
            "type": "response",
            "content": full_response,
            "done": True,
        }
        if citations:
            msg["citations"] = citations
        if conversation_id is not None:
            msg["conversation_id"] = str(conversation_id)
        await self._send(msg)

    # ------------------------------------------------------------------ #
    # Error events
    # ------------------------------------------------------------------ #

    async def emit_error(self, message: str) -> None:
        """Send an error message to the client.

        Args:
            message: Human-readable error description.
        """
        await self._send({
            "type": "error",
            "message": message,
        })

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _send_status(self, status: str) -> None:
        """Send a plain status message."""
        await self._send({"type": "status", "status": status})

    async def _send(self, message: dict[str, Any]) -> None:
        """Route the message through the ConnectionManager.

        Primary routing: always send to the user's direct connection(s).
        Secondary routing: also broadcast to the conversation scope if set,
        so that other connections monitoring the same conversation receive it.

        Sending to the user directly is always the reliable path because the
        connection is registered in the user index at connect() time. The
        conversation index is only populated if the socket was opened with a
        conversation_id or if update_conversation() is called later.
        """
        try:
            await self._manager.send_to_user(
                tenant_id=self._tenant_id,
                user_id=self._user_id,
                message=message,
            )
        except Exception as exc:
            log.warning("ws.event_send_failed", error=str(exc), msg_type=message.get("type"))
