"""WebSocket chat endpoint - ws /api/v1/ws/chat

Provides real-time bidirectional chat with the agent runtime via WebSocket.
Complements the existing SSE streaming endpoint (/api/v1/chat/stream).

Connection lifecycle:
1. Client connects to ws://host/api/v1/ws/chat[?token=<jwt>]
2. authenticate_websocket() validates the token (query param or first message)
3. Connection is registered with ConnectionManager
4. Message loop: receive client messages, process with AgentRuntime, stream back
5. On disconnect/error, connection is cleaned up from manager

Client -> Server message types:
    {"type": "message", "content": "...", "conversation_id": "..."}  - chat turn
    {"type": "ping"}  - keepalive (server responds with {"type": "pong"})
    {"type": "auth", "token": "..."}  - initial auth (if token not in query param)

Server -> Client message types:
    {"type": "status",   "status": "thinking|searching|generating"}
    {"type": "response", "content": "...", "done": false}  - streaming chunk
    {"type": "response", "content": "...", "done": true,   - final chunk
                         "citations": [...], "conversation_id": "..."}
    {"type": "error",    "message": "..."}
    {"type": "pong"}     - response to ping

Streaming strategy:
    The current AgentRuntime.chat() is non-streaming (returns full response).
    The emitter sends status events, then the complete response as done=True.
    When the runtime is upgraded to support token streaming, emit_response_chunk()
    can be called per token; the protocol is already designed for it.

Security:
    - Auth is enforced on every connection before entering the message loop.
    - Tenant isolation: ConnectionManager routes by (tenant_id, user_id).
    - Rate limiting and RBAC are checked per chat message (same as HTTP endpoint).
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient, LLMError, LLMRateLimitError
from src.agent.runtime import AgentRuntime, ChatRequest
from src.config import Settings, get_settings
from src.core.policy import Permission, check_permission
from src.core.rate_limit import RateLimiter, get_rate_limiter
from src.database import get_db_session
from src.websocket.auth import authenticate_websocket
from src.websocket.events import AgentEventEmitter
from src.websocket.manager import ConnectionManager, get_connection_manager

log = structlog.get_logger(__name__)

ws_router = APIRouter(prefix="/api/v1/ws", tags=["websocket"])


@ws_router.websocket("/chat")
async def ws_chat(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    """Main WebSocket endpoint for real-time agent chat.

    Accepts WebSocket connections and handles bidirectional message exchange
    with the agent runtime.
    """
    # ------------------------------------------------------------------ #
    # 1. Accept the WebSocket connection (required before any read/write)
    # ------------------------------------------------------------------ #
    await websocket.accept()

    # ------------------------------------------------------------------ #
    # 2. Authenticate - validates JWT, provisions user if needed
    # ------------------------------------------------------------------ #
    current_user = await authenticate_websocket(
        websocket, db=db, settings=settings
    )
    if current_user is None:
        # authenticate_websocket already closed the socket with code 4001
        return

    # ------------------------------------------------------------------ #
    # 3. Register with ConnectionManager
    # ------------------------------------------------------------------ #
    manager: ConnectionManager = get_connection_manager()
    await manager.connect(
        websocket,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
    )

    log.info(
        "ws.session_started",
        user_id=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
    )

    # ------------------------------------------------------------------ #
    # 4. Message loop
    # ------------------------------------------------------------------ #
    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                log.info("ws.client_disconnected", user_id=str(current_user.id))
                break
            except Exception as exc:
                log.warning("ws.receive_error", error=str(exc))
                break

            msg_type = raw.get("type") if isinstance(raw, dict) else None

            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if msg_type == "message":
                await _handle_chat_message(
                    websocket=websocket,
                    raw_message=raw,
                    current_user=current_user,
                    db=db,
                    settings=settings,
                    rate_limiter=rate_limiter,
                    manager=manager,
                )
                continue

            # Unknown message type
            await websocket.send_json({
                "type": "error",
                "message": f"Unknown message type: {msg_type!r}",
            })

    except WebSocketDisconnect:
        log.info("ws.session_ended_disconnect", user_id=str(current_user.id))

    except Exception as exc:
        log.error("ws.session_error", error=str(exc), exc_info=True)
        try:
            await websocket.send_json({"type": "error", "message": "Internal server error"})
        except Exception:
            pass

    finally:
        # ------------------------------------------------------------------ #
        # 5. Cleanup - always remove from connection registry
        # ------------------------------------------------------------------ #
        await manager.disconnect(websocket)
        log.info("ws.session_cleaned_up", user_id=str(current_user.id))


async def _handle_chat_message(
    *,
    websocket: WebSocket,
    raw_message: dict[str, Any],
    current_user: Any,
    db: AsyncSession,
    settings: Settings,
    rate_limiter: RateLimiter,
    manager: ConnectionManager,
) -> None:
    """Process a single chat message and stream the response back.

    Args:
        websocket: The active WebSocket connection.
        raw_message: Parsed JSON message dict from the client.
        current_user: Authenticated user context.
        db: Database session.
        settings: Application settings.
        rate_limiter: Rate limiter for this user.
        manager: ConnectionManager for event routing.
    """
    content = raw_message.get("content", "").strip()
    if not content:
        await websocket.send_json({
            "type": "error",
            "message": "Message content is required",
        })
        return

    if len(content) > 32_000:
        await websocket.send_json({
            "type": "error",
            "message": "Message content exceeds maximum length of 32,000 characters",
        })
        return

    # Parse optional conversation_id
    raw_conv_id = raw_message.get("conversation_id")
    conversation_id: uuid.UUID | None = None
    if raw_conv_id:
        try:
            conversation_id = uuid.UUID(str(raw_conv_id))
        except ValueError:
            await websocket.send_json({
                "type": "error",
                "message": "Invalid conversation_id format",
            })
            return

    # ------------------------------------------------------------------ #
    # Rate limit check
    # ------------------------------------------------------------------ #
    try:
        await rate_limiter.check(current_user.id)
    except Exception as exc:
        await websocket.send_json({
            "type": "error",
            "message": "Rate limit exceeded. Please wait before sending another message.",
        })
        log.warning("ws.rate_limited", user_id=str(current_user.id), error=str(exc))
        return

    # ------------------------------------------------------------------ #
    # Permission check
    # ------------------------------------------------------------------ #
    try:
        check_permission(current_user.role, Permission.CHAT_SEND)
    except Exception:
        await websocket.send_json({
            "type": "error",
            "message": "Insufficient permissions to send chat messages",
        })
        return

    # ------------------------------------------------------------------ #
    # Set up event emitter for this conversation turn
    # ------------------------------------------------------------------ #
    emitter = AgentEventEmitter(
        manager=manager,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        conversation_id=conversation_id,
    )

    # ------------------------------------------------------------------ #
    # Process with AgentRuntime
    # ------------------------------------------------------------------ #
    await emitter.emit_agent_started()

    runtime = AgentRuntime(
        db=db,
        settings=settings,
        llm_client=LLMClient(settings),
    )

    try:
        await emitter.emit_thinking("Retrieving context and preparing response")

        chat_response = await runtime.chat(
            user=current_user.user,
            request=ChatRequest(
                message=content,
                conversation_id=conversation_id,
            ),
        )

        await emitter.emit_generating()

        # Send the completed response
        await emitter.emit_agent_completed(
            chat_response.response,
            citations=[c for c in chat_response.citations],
            conversation_id=chat_response.conversation_id,
        )

        log.info(
            "ws.chat_complete",
            user_id=str(current_user.id),
            conversation_id=str(chat_response.conversation_id),
            latency_ms=chat_response.latency_ms,
        )

    except LLMRateLimitError as exc:
        log.warning("ws.llm_rate_limited", error=str(exc))
        await emitter.emit_error(
            "The AI service is currently rate limited. Please try again in a moment."
        )

    except LLMError as exc:
        log.error("ws.llm_error", error=str(exc))
        await emitter.emit_error("AI service error. Please try again.")

    except Exception as exc:
        log.error("ws.chat_error", error=str(exc), exc_info=True)
        await emitter.emit_error("An unexpected error occurred while processing your message.")
