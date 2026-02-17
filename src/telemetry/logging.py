"""Structured logging configuration for production observability.

Configures structlog with JSON output, trace correlation, and request tracking.

Features:
- JSON-formatted logs in production (human-readable in dev)
- Trace ID and span ID from OpenTelemetry context
- Request ID propagation through middleware
- Tenant ID and user ID in all log entries
- ISO8601 timestamps with timezone
- Stack traces for exceptions
- Performance-optimized processors

Log format (production):
    {
        "timestamp": "2026-02-17T10:30:45.123456Z",
        "level": "info",
        "event": "agent.execute.started",
        "trace_id": "abc123...",
        "span_id": "def456...",
        "request_id": "req_789...",
        "tenant_id": "tenant_uuid",
        "user_id": "user_uuid",
        "agent_id": "agent_uuid",
        "message": "Starting agent execution"
    }
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

import structlog
from structlog.types import EventDict, Processor

try:
    from opentelemetry import trace
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False


def add_trace_context(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add OpenTelemetry trace and span IDs to log entries.

    Args:
        logger: Logger instance
        method_name: Method name being called
        event_dict: Event dictionary to enhance

    Returns:
        Enhanced event dictionary with trace context
    """
    if not OTEL_AVAILABLE:
        return event_dict

    try:
        span = trace.get_current_span()
        if span.is_recording():
            ctx = span.get_span_context()
            if ctx.is_valid:
                event_dict["trace_id"] = format(ctx.trace_id, "032x")
                event_dict["span_id"] = format(ctx.span_id, "016x")
    except Exception:
        # Don't fail logging if trace context unavailable
        pass

    return event_dict


def add_request_context(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add request-scoped context to log entries.

    This processor looks for context variables set by middleware:
    - request_id: Unique identifier for this request
    - tenant_id: Current tenant context
    - user_id: Current user context

    Args:
        logger: Logger instance
        method_name: Method name being called
        event_dict: Event dictionary to enhance

    Returns:
        Enhanced event dictionary with request context
    """
    # These are set by RequestIdMiddleware and AuthMiddleware
    # structlog automatically propagates context variables
    return event_dict


def configure_logging(
    *,
    json_logs: bool = False,
    log_level: str = "INFO",
) -> None:
    """Configure structured logging for the application.

    Args:
        json_logs: Use JSON format (True for production, False for dev)
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    # Configure stdlib logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper()),
    )

    # Shared processors for all environments
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,  # Merge context variables
        structlog.stdlib.add_log_level,  # Add log level
        structlog.stdlib.add_logger_name,  # Add logger name
        add_trace_context,  # Add OpenTelemetry trace/span IDs
        add_request_context,  # Add request_id, tenant_id, user_id
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # ISO8601 timestamps
        structlog.processors.StackInfoRenderer(),  # Stack traces
    ]

    if json_logs:
        # Production: JSON output
        processors = shared_processors + [
            structlog.processors.format_exc_info,  # Format exceptions
            structlog.processors.JSONRenderer(),  # JSON output
        ]
    else:
        # Development: Human-readable output with colors
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(  # Pretty console output
                colors=True,
                exception_formatter=structlog.dev.RichTracebackFormatter(),
            ),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ------------------------------------------------------------------ #
# Request ID Middleware
# ------------------------------------------------------------------ #


class RequestIdMiddleware:
    """Middleware that generates and propagates request IDs.

    Adds a unique request_id to each request's context variables,
    which are then included in all log entries for that request.

    The request_id is also added as a response header for correlation.
    """

    def __init__(self, app: Any) -> None:
        """Initialize middleware.

        Args:
            app: ASGI application
        """
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Process request with request ID.

        Args:
            scope: ASGI scope
            receive: ASGI receive channel
            send: ASGI send channel
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Generate request ID
        request_id = f"req_{uuid.uuid4().hex[:16]}"

        # Add to structlog context for this request
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # Add request_id to response headers
        async def send_with_request_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_request_id)


# ------------------------------------------------------------------ #
# Context Binding Helpers
# ------------------------------------------------------------------ #


def bind_tenant_context(tenant_id: str | uuid.UUID) -> None:
    """Bind tenant ID to log context for this request.

    Args:
        tenant_id: Tenant identifier
    """
    structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id))


def bind_user_context(user_id: str | uuid.UUID) -> None:
    """Bind user ID to log context for this request.

    Args:
        user_id: User identifier
    """
    structlog.contextvars.bind_contextvars(user_id=str(user_id))


def bind_agent_context(agent_id: str | uuid.UUID, agent_type: str) -> None:
    """Bind agent context to logs for this execution.

    Args:
        agent_id: Agent identifier
        agent_type: Agent type
    """
    structlog.contextvars.bind_contextvars(
        agent_id=str(agent_id),
        agent_type=agent_type,
    )


def clear_context() -> None:
    """Clear all context variables (useful for testing)."""
    structlog.contextvars.clear_contextvars()
