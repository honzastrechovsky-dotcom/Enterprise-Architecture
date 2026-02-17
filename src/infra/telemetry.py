"""
OpenTelemetry distributed tracing and metrics for agent platform.

Provides comprehensive observability for:
- HTTP request tracing
- Database query spans
- LLM API call tracing (tokens, latency, cost estimation)
- Agent execution spans (including nested agent calls)
- Tool execution tracing
- Custom metrics (request duration, token usage, etc.)

Key features:
- OTLP export (works with Jaeger, Tempo, etc.)
- Trace context propagation across services
- Custom span attributes (tenant_id, user_id, agent_id)
- Graceful degradation if collector unavailable
- Zero overhead when telemetry disabled
- Async-compatible spans

Architecture:
- TracerProvider configured at startup
- TracingMiddleware instruments HTTP requests
- Manual spans for domain operations (DB, LLM, agent execution)
- Context propagation via OpenTelemetry context API

Example:
    # Initialize at startup
    setup_telemetry(settings)

    # HTTP requests automatically traced by middleware

    # Manual spans for custom operations
    with create_span("agent.execute", attributes={"agent_id": "123"}) as span:
        result = await agent.execute()
        span.set_attribute("token_count", result.token_count)
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import structlog
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Span, Status, StatusCode, Tracer
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    trace = Any  # type: ignore
    Span = Any  # type: ignore
    Tracer = Any  # type: ignore
    Status = Any  # type: ignore
    StatusCode = Any  # type: ignore

from src.config import Settings

log = structlog.get_logger(__name__)


_tracer: Tracer | None = None
_enabled: bool = False


def setup_telemetry(settings: Settings) -> None:
    """
    Initialize OpenTelemetry tracing.

    Configures TracerProvider, OTLP exporter, and SQLAlchemy instrumentation.
    If telemetry is disabled or OpenTelemetry not installed, this is a no-op.

    Args:
        settings: Application settings with OTLP endpoint configuration
    """
    global _tracer, _enabled

    if not settings.enable_telemetry:
        log.info("telemetry.disabled")
        return

    if not OTEL_AVAILABLE:
        log.warning("telemetry.opentelemetry_not_installed")
        return

    try:
        # Configure resource attributes
        resource = Resource.create({
            "service.name": "enterprise-agent-platform",
            "service.version": "0.1.0",
            "deployment.environment": settings.environment.value,
        })

        # Create tracer provider
        provider = TracerProvider(resource=resource)

        # Add OTLP exporter
        otlp_exporter = OTLPSpanExporter(
            endpoint=settings.otlp_endpoint,
            insecure=settings.is_dev,  # Use insecure connection in dev
        )
        span_processor = BatchSpanProcessor(otlp_exporter)
        provider.add_span_processor(span_processor)

        # Set as global provider
        trace.set_tracer_provider(provider)

        # Instrument SQLAlchemy for automatic DB span creation
        SQLAlchemyInstrumentor().instrument()

        # Instrument httpx for automatic HTTP client tracing
        HTTPXClientInstrumentor().instrument()

        # Note: FastAPI instrumentation is done in main.py after app creation

        _tracer = trace.get_tracer(__name__)
        _enabled = True

        log.info(
            "telemetry.initialized",
            otlp_endpoint=settings.otlp_endpoint,
            environment=settings.environment,
        )

    except Exception as exc:
        log.error(
            "telemetry.setup_failed",
            error=str(exc),
            fallback="disabled",
        )
        _enabled = False


def is_enabled() -> bool:
    """Check if telemetry is active."""
    return _enabled


def instrument_fastapi(app: Any) -> None:
    """Instrument FastAPI application with OpenTelemetry.

    Must be called after app creation but before first request.

    Args:
        app: FastAPI application instance
    """
    if not _enabled or not OTEL_AVAILABLE:
        return

    try:
        FastAPIInstrumentor.instrument_app(app)
        log.info("telemetry.fastapi_instrumented")
    except Exception as exc:
        log.error("telemetry.fastapi_instrumentation_failed", error=str(exc))


def get_tracer() -> Tracer | None:
    """Return configured tracer or None if telemetry disabled."""
    return _tracer if _enabled else None


@contextmanager
def create_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
    kind: Any = None,
) -> Generator[Span | None, None, None]:
    """
    Create a tracing span with custom attributes.

    Returns a no-op context manager if telemetry is disabled.

    Args:
        name: Span name (use dotted notation: "agent.execute")
        attributes: Custom span attributes
        kind: Span kind (SERVER, CLIENT, INTERNAL, etc.)

    Example:
        with create_span("llm.embed", attributes={"model": "text-embedding-3-small"}) as span:
            embeddings = await llm.embed(texts)
            if span:
                span.set_attribute("input_count", len(texts))
    """
    if not _enabled or _tracer is None:
        yield None
        return

    try:
        with _tracer.start_as_current_span(name, kind=kind) as span:
            if attributes:
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            yield span
    except Exception as exc:
        log.error("telemetry.span_failed", span_name=name, error=str(exc))
        yield None


def record_exception(span: Span | None, exception: Exception) -> None:
    """
    Record exception on span.

    No-op if span is None or telemetry disabled.

    Args:
        span: Span to record exception on
        exception: Exception to record
    """
    if span is not None and _enabled:
        try:
            span.set_status(Status(StatusCode.ERROR))
            span.record_exception(exception)
        except Exception as exc:
            log.error("telemetry.record_exception_failed", error=str(exc))


class TracingMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware for automatic HTTP request tracing.

    Creates a span for each HTTP request with:
    - HTTP method, path, status code
    - Request duration
    - User ID and tenant ID (if available)
    - Error status on exceptions

    Integrates with OpenTelemetry context propagation for distributed tracing.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Process request with tracing."""
        if not _enabled or _tracer is None:
            return await call_next(request)

        span_name = f"{request.method} {request.url.path}"

        try:
            with _tracer.start_as_current_span(
                span_name,
                kind=trace.SpanKind.SERVER,
            ) as span:
                # Add HTTP attributes
                span.set_attribute("http.method", request.method)
                span.set_attribute("http.url", str(request.url))
                span.set_attribute("http.scheme", request.url.scheme)

                # Add user context if available
                if hasattr(request.state, "user_id"):
                    span.set_attribute("user.id", str(request.state.user_id))
                if hasattr(request.state, "tenant_id"):
                    span.set_attribute("tenant.id", str(request.state.tenant_id))

                start_time = time.perf_counter()

                try:
                    response = await call_next(request)

                    # Record response status
                    span.set_attribute("http.status_code", response.status_code)

                    # Set span status based on HTTP status
                    if response.status_code >= 500 or response.status_code >= 400:
                        span.set_status(Status(StatusCode.ERROR))
                    else:
                        span.set_status(Status(StatusCode.OK))

                    return response

                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(exc)
                    raise

                finally:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    span.set_attribute("http.duration_ms", round(duration_ms, 2))

        except Exception as exc:
            log.error(
                "telemetry.middleware_failed",
                path=request.url.path,
                error=str(exc),
            )
            return await call_next(request)


def trace_llm_call(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: float,
    operation: str = "generate",
) -> None:
    """
    Create span for LLM API call with token usage.

    Args:
        model: LLM model identifier
        prompt_tokens: Input token count
        completion_tokens: Output token count
        duration_ms: Call duration in milliseconds
        operation: Operation type (generate, embed, etc.)
    """
    if not _enabled or _tracer is None:
        return

    try:
        span_name = f"llm.{operation}"
        with _tracer.start_as_current_span(span_name) as span:
            span.set_attribute("llm.model", model)
            span.set_attribute("llm.operation", operation)
            span.set_attribute("llm.prompt_tokens", prompt_tokens)
            span.set_attribute("llm.completion_tokens", completion_tokens)
            span.set_attribute("llm.total_tokens", prompt_tokens + completion_tokens)
            span.set_attribute("llm.duration_ms", round(duration_ms, 2))

    except Exception as exc:
        log.error("telemetry.trace_llm_call_failed", error=str(exc))


def trace_agent_execution(
    *,
    agent_id: str,
    agent_type: str,
    conversation_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> Any:
    """
    Create span for agent execution.

    Returns context manager that yields span.

    Args:
        agent_id: Agent identifier
        agent_type: Agent type (rag_agent, skill_agent, etc.)
        conversation_id: Conversation context
        tenant_id: Tenant identifier
        user_id: User identifier

    Example:
        with trace_agent_execution(agent_id="123", agent_type="rag") as span:
            result = await agent.execute()
            if span:
                span.set_attribute("tool_count", len(result.tools_used))
    """
    if not _enabled or _tracer is None:
        return contextmanager(lambda: (yield None))()

    try:
        span = _tracer.start_as_current_span(
            f"agent.execute.{agent_type}",
            kind=trace.SpanKind.INTERNAL,
        )
        span.__enter__()

        span.set_attribute("agent.id", agent_id)
        span.set_attribute("agent.type", agent_type)
        if conversation_id:
            span.set_attribute("conversation.id", conversation_id)
        if tenant_id:
            span.set_attribute("tenant.id", tenant_id)
        if user_id:
            span.set_attribute("user.id", user_id)

        return contextmanager(lambda: (yield span))()

    except Exception as exc:
        log.error("telemetry.trace_agent_failed", error=str(exc))
        return contextmanager(lambda: (yield None))()


def trace_tool_execution(
    *,
    tool_name: str,
    parameters: dict[str, Any] | None = None,
) -> Any:
    """
    Create span for tool execution.

    Args:
        tool_name: Tool identifier
        parameters: Tool parameters (will be truncated if large)

    Example:
        with trace_tool_execution(tool_name="web_search", parameters={"query": "..."}) as span:
            result = await tool.execute()
            if span:
                span.set_attribute("result_count", len(result))
    """
    if not _enabled or _tracer is None:
        return contextmanager(lambda: (yield None))()

    try:
        span = _tracer.start_as_current_span(
            f"tool.{tool_name}",
            kind=trace.SpanKind.INTERNAL,
        )
        span.__enter__()

        span.set_attribute("tool.name", tool_name)

        if parameters:
            # Truncate parameters to avoid huge spans
            params_str = str(parameters)[:500]
            span.set_attribute("tool.parameters", params_str)

        return contextmanager(lambda: (yield span))()

    except Exception as exc:
        log.error("telemetry.trace_tool_failed", error=str(exc))
        return contextmanager(lambda: (yield None))()
