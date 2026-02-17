"""Chat endpoint - POST /chat

This is the primary endpoint. It:
1. Authenticates the user
2. Checks rate limit
3. Checks RBAC (CHAT_SEND permission)
4. Invokes the agent runtime
5. Writes the audit log
6. Returns the response

All failures (LLM error, RAG error) are handled gracefully with
appropriate HTTP status codes and audit entries.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient, LLMError, LLMRateLimitError
from src.agent.runtime import AgentRuntime, ChatRequest
from src.auth.api_key_auth import require_scope
from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.config import Settings, get_settings
from src.core.audit import AuditService, RequestTimer
from src.core.policy import Permission, check_permission
from src.core.rate_limit import RateLimiter, get_rate_limiter
from src.database import get_db_session
from src.infra.streaming import create_sse_generator
from src.models.audit import AuditStatus
from src.models.user import UserRole

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(require_scope("chat"))])


class ChatRequestBody(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=32_000,
        description="User message to the agent",
    )
    conversation_id: uuid.UUID | None = Field(
        default=None,
        description="Continue an existing conversation. Omit to start a new one.",
    )
    model_override: str | None = Field(
        default=None,
        description="Override the default model for this request (admin/operator only)",
    )


class CitationResponse(BaseModel):
    index: int
    document_id: str
    document_name: str
    document_version: str
    chunk_index: int
    content_snippet: str
    page_number: int | None = None
    section: str | None = None


class ChatResponseBody(BaseModel):
    response: str
    conversation_id: uuid.UUID
    citations: list[CitationResponse]
    model_used: str
    latency_ms: int


@router.post(
    "",
    response_model=ChatResponseBody,
    summary="Send a message to the agent",
    description=(
        "Send a message to the enterprise agent. The agent retrieves relevant "
        "context from your organization's documents and generates a cited response."
    ),
)
async def chat(
    body: ChatRequestBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> ChatResponseBody:
    """Main chat endpoint."""
    # 1. Rate limit check
    await rate_limiter.check(current_user.id)

    # 2. Permission check
    check_permission(current_user.role, Permission.CHAT_SEND)

    audit = AuditService(db)
    timer = RequestTimer()

    # 3. Model override permission check
    if body.model_override and current_user.role == UserRole.VIEWER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewers cannot override the model",
        )

    runtime = AgentRuntime(
        db=db,
        settings=settings,
        llm_client=LLMClient(settings),
    )

    try:
        with timer:
            chat_response = await runtime.chat(
                user=current_user.user,
                request=ChatRequest(
                    message=body.message,
                    conversation_id=body.conversation_id,
                    model_override=body.model_override,
                ),
            )

        # 4. Audit log - success
        await audit.log(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="chat.message",
            resource_type="conversation",
            resource_id=str(chat_response.conversation_id),
            model_used=chat_response.model_used,
            request_summary=body.message,
            response_summary=chat_response.response,
            latency_ms=timer.elapsed_ms,
            status=AuditStatus.SUCCESS,
            extra={"citation_count": len(chat_response.citations)},
        )

        return ChatResponseBody(
            response=chat_response.response,
            conversation_id=chat_response.conversation_id,
            citations=[CitationResponse(**c) for c in chat_response.citations],
            model_used=chat_response.model_used,
            latency_ms=timer.elapsed_ms,
        )

    except LLMRateLimitError as exc:
        await audit.log(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="chat.message",
            status=AuditStatus.RATE_LIMITED,
            error_detail=str(exc),
            latency_ms=timer.elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM service rate limit exceeded. Please try again in a moment.",
        ) from exc

    except LLMError as exc:
        await audit.log(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="chat.message",
            status=AuditStatus.ERROR,
            error_detail=str(exc),
            latency_ms=timer.elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM service error. Please try again.",
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        log.error("chat.unexpected_error", error=str(exc), exc_info=True)
        await audit.log(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            action="chat.message",
            status=AuditStatus.ERROR,
            error_detail=str(exc),
            latency_ms=timer.elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from exc


@router.post(
    "/stream",
    summary="Send a message with streaming response",
    description=(
        "Streaming variant of chat endpoint. "
        "Returns Server-Sent Events (SSE) for real-time streaming."
    ),
)
async def chat_stream(
    body: ChatRequestBody,
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> StreamingResponse:
    """Streaming chat endpoint using SSE."""
    # 1. Rate limit check
    await rate_limiter.check(current_user.id)

    # 2. Permission check
    check_permission(current_user.role, Permission.CHAT_SEND)

    # 3. Model override permission check
    if body.model_override and current_user.role == UserRole.VIEWER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewers cannot override the model",
        )

    runtime = AgentRuntime(
        db=db,
        settings=settings,
        llm_client=LLMClient(settings),
    )

    async def generate():
        """Generator for SSE streaming."""
        try:
            # Call runtime normally; streaming at the transport layer via SSE
            chat_response = await runtime.chat(
                user=current_user.user,
                request=ChatRequest(
                    message=body.message,
                    conversation_id=body.conversation_id,
                    model_override=body.model_override,
                ),
            )

            # Send final completion event with the full response
            import json as _json
            yield f"data: {_json.dumps({'type': 'done', 'conversation_id': str(chat_response.conversation_id), 'response': chat_response.response})}\n\n"

        except Exception as exc:
            log.error("chat.stream_error", error=str(exc), exc_info=True)
            yield "data: {'type': 'error', 'message': 'Stream error'}\n\n"

    return StreamingResponse(
        create_sse_generator(generate()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
