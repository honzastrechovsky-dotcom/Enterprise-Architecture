"""
Server-Sent Events (SSE) streaming for real-time agent output.

Provides streaming response capability for LLM token-by-token output,
agent status updates, and citations during execution. Compatible with
FastAPI's StreamingResponse.

Key features:
- SSE format (text/event-stream)
- Multiple event types (token, status, citation, error)
- Stream aggregation (collect full response while streaming)
- Client disconnect detection
- Heartbeat to keep connection alive
- UTF-8 safe encoding

Event types:
- token: Individual LLM output tokens
- status: Agent execution status updates
- citation: Source citations as they're found
- error: Execution errors
- done: Stream completion signal

Design:
- Generator pattern for async iteration
- Buffers full response for post-stream storage
- Handles backpressure via asyncio.Queue
- Graceful disconnect handling

Example:
    async def stream_agent_response():
        stream = AgentOutputStream()

        async def generate():
            async for chunk in agent.execute_streaming():
                await stream.emit_token(chunk)
                yield stream.format_event("token", chunk)
            yield stream.format_event("done", "")

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
        )
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from fastapi.responses import StreamingResponse as FastAPIStreamingResponse

log = structlog.get_logger(__name__)


class EventType(StrEnum):
    """SSE event types for agent streaming."""
    TOKEN = "token"
    STATUS = "status"
    CITATION = "citation"
    ERROR = "error"
    DONE = "done"
    HEARTBEAT = "heartbeat"


@dataclass
class AgentStreamEvent:
    """
    A single event in the agent output stream.

    Attributes:
        type: Event type (token, status, citation, error, done)
        data: Event payload (string or dict)
        timestamp: ISO 8601 timestamp
        metadata: Additional context (agent_id, conversation_id, etc.)
    """
    type: EventType
    data: str | dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "type": self.type,
            "data": self.data,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    def to_sse(self) -> str:
        """
        Format as SSE message.

        SSE format:
            event: {type}
            data: {json}

        """
        json_data = json.dumps(self.to_dict())
        return f"event: {self.type}\ndata: {json_data}\n\n"


class AgentOutputStream:
    """
    Manages streaming agent output with aggregation and disconnect handling.

    Collects full response text while streaming tokens, maintains citation
    list, and handles client disconnects gracefully.

    Example:
        stream = AgentOutputStream(
            agent_id="agent-123",
            conversation_id="conv-456",
        )

        await stream.emit_token("Hello")
        await stream.emit_token(" world")
        await stream.emit_status("thinking", "Analyzing context...")
        await stream.emit_citation({"document_id": "doc-789", "relevance": 0.95})
        await stream.emit_done()

        full_response = stream.get_full_response()
        citations = stream.get_citations()
    """

    def __init__(
        self,
        *,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        buffer_size: int = 1000,
    ) -> None:
        """
        Initialize output stream.

        Args:
            agent_id: Agent identifier for metadata
            conversation_id: Conversation identifier for metadata
            buffer_size: Max events to buffer before backpressure
        """
        self._agent_id = agent_id
        self._conversation_id = conversation_id
        self._buffer: asyncio.Queue[AgentStreamEvent] = asyncio.Queue(maxsize=buffer_size)
        self._full_response: list[str] = []
        self._citations: list[dict[str, Any]] = []
        self._disconnected = False
        self._start_time = time.monotonic()

        log.info(
            "stream.created",
            agent_id=agent_id,
            conversation_id=conversation_id,
        )

    def _make_metadata(self, **extra: Any) -> dict[str, Any]:
        """Build event metadata with agent/conversation context."""
        meta = {}
        if self._agent_id:
            meta["agent_id"] = self._agent_id
        if self._conversation_id:
            meta["conversation_id"] = self._conversation_id
        meta.update(extra)
        return meta

    async def emit_token(self, token: str) -> None:
        """
        Emit a single LLM output token.

        Aggregates tokens into full_response buffer for storage.
        """
        if self._disconnected:
            return

        event = AgentStreamEvent(
            type=EventType.TOKEN,
            data=token,
            metadata=self._make_metadata(),
        )
        self._full_response.append(token)
        await self._buffer.put(event)

    async def emit_status(self, status: str, message: str) -> None:
        """
        Emit agent status update.

        Args:
            status: Status identifier (thinking, executing, retrieving)
            message: Human-readable status message
        """
        if self._disconnected:
            return

        event = AgentStreamEvent(
            type=EventType.STATUS,
            data={"status": status, "message": message},
            metadata=self._make_metadata(),
        )
        await self._buffer.put(event)

    async def emit_citation(self, citation: dict[str, Any]) -> None:
        """
        Emit source citation.

        Args:
            citation: Citation metadata (document_id, relevance, etc.)
        """
        if self._disconnected:
            return

        event = AgentStreamEvent(
            type=EventType.CITATION,
            data=citation,
            metadata=self._make_metadata(),
        )
        self._citations.append(citation)
        await self._buffer.put(event)

    async def emit_error(self, error: str) -> None:
        """Emit error event and mark stream as terminated."""
        if self._disconnected:
            return

        event = AgentStreamEvent(
            type=EventType.ERROR,
            data={"error": error},
            metadata=self._make_metadata(),
        )
        await self._buffer.put(event)
        self._disconnected = True

    async def emit_done(self) -> None:
        """Emit stream completion event."""
        if self._disconnected:
            return

        duration = time.monotonic() - self._start_time
        event = AgentStreamEvent(
            type=EventType.DONE,
            data={"duration_seconds": round(duration, 2)},
            metadata=self._make_metadata(
                token_count=len(self._full_response),
                citation_count=len(self._citations),
            ),
        )
        await self._buffer.put(event)
        self._disconnected = True

        log.info(
            "stream.completed",
            agent_id=self._agent_id,
            conversation_id=self._conversation_id,
            token_count=len(self._full_response),
            duration_seconds=round(duration, 2),
        )

    def mark_disconnected(self) -> None:
        """Mark stream as disconnected (client dropped connection)."""
        self._disconnected = True
        log.warning(
            "stream.client_disconnected",
            agent_id=self._agent_id,
            conversation_id=self._conversation_id,
        )

    def get_full_response(self) -> str:
        """Return aggregated response text."""
        return "".join(self._full_response)

    def get_citations(self) -> list[dict[str, Any]]:
        """Return all emitted citations."""
        return list(self._citations)

    async def __aiter__(self) -> AsyncGenerator[AgentStreamEvent, None]:
        """Async iterator over events."""
        while not self._disconnected or not self._buffer.empty():
            try:
                event = await asyncio.wait_for(self._buffer.get(), timeout=1.0)
                yield event
            except TimeoutError:
                # Emit heartbeat to keep connection alive
                if not self._disconnected:
                    yield AgentStreamEvent(
                        type=EventType.HEARTBEAT,
                        data="",
                        metadata=self._make_metadata(),
                    )


async def create_sse_generator(
    stream: AgentOutputStream,
    *,
    heartbeat_interval: float = 15.0,
) -> AsyncGenerator[str, None]:
    """
    Create SSE generator from AgentOutputStream.

    Formats events as SSE messages and sends heartbeats to prevent timeout.

    Args:
        stream: AgentOutputStream to read from
        heartbeat_interval: Seconds between heartbeat comments

    Yields:
        SSE-formatted strings
    """
    last_heartbeat = time.monotonic()

    try:
        async for event in stream:
            yield event.to_sse()

            # Send heartbeat comment if needed
            now = time.monotonic()
            if now - last_heartbeat > heartbeat_interval:
                yield ": heartbeat\n\n"
                last_heartbeat = now

    except asyncio.CancelledError:
        # Client disconnected
        stream.mark_disconnected()
        log.info("stream.cancelled")
        raise


def StreamingResponse(
    generator: AsyncGenerator[str, None],
    **kwargs: Any,
) -> FastAPIStreamingResponse:
    """
    Create FastAPI StreamingResponse for SSE.

    Wrapper around FastAPI's StreamingResponse with SSE headers.

    Args:
        generator: Async generator yielding SSE strings
        **kwargs: Additional StreamingResponse arguments

    Returns:
        FastAPI StreamingResponse configured for SSE
    """
    return FastAPIStreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
        **kwargs,
    )
