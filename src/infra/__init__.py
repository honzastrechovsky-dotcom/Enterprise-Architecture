"""
Infrastructure components for background processing, rate limiting,
streaming, telemetry, and health checks.

This package contains enterprise-grade infrastructure for:
- Background task processing (async worker pools)
- Redis-backed distributed rate limiting
- SSE streaming for real-time agent output
- OpenTelemetry distributed tracing
- Enhanced health checks for Kubernetes

All components are designed for multi-tenant, on-premise deployment.
"""

from __future__ import annotations

from src.infra.background_worker import BackgroundWorkerPool, Task, TaskStatus, TaskType
from src.infra.health import (
    ComponentHealth,
    ComponentStatus,
    HealthCheck,
    HealthCheckRouter,
    SystemHealth,
)
from src.infra.redis_rate_limiter import RedisRateLimiter
from src.infra.streaming import (
    AgentOutputStream,
    AgentStreamEvent,
    EventType,
    StreamingResponse,
    create_sse_generator,
)
from src.infra.telemetry import (
    TracingMiddleware,
    create_span,
    get_tracer,
    instrument_fastapi,
    is_enabled,
    record_exception,
    setup_telemetry,
    trace_agent_execution,
    trace_llm_call,
    trace_tool_execution,
)

__all__ = [
    # Background worker
    "BackgroundWorkerPool",
    "Task",
    "TaskStatus",
    "TaskType",
    # Health checks
    "ComponentHealth",
    "ComponentStatus",
    "HealthCheck",
    "HealthCheckRouter",
    "SystemHealth",
    # Rate limiting
    "RedisRateLimiter",
    # Streaming
    "AgentOutputStream",
    "AgentStreamEvent",
    "EventType",
    "StreamingResponse",
    "create_sse_generator",
    # Telemetry
    "create_span",
    "get_tracer",
    "instrument_fastapi",
    "is_enabled",
    "record_exception",
    "setup_telemetry",
    "trace_agent_execution",
    "trace_llm_call",
    "trace_tool_execution",
    "TracingMiddleware",
]
