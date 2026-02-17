"""Prometheus metrics endpoint and instrumentation.

Exports metrics in Prometheus exposition format for scraping by Prometheus server.
Integrates with existing MetricsCollector to provide dual-export (DB + Prometheus).

Metrics exported:
- http_requests_total: Counter of HTTP requests by method, endpoint, status
- http_request_duration_seconds: Histogram of HTTP request latencies
- llm_requests_total: Counter of LLM requests by model, status
- llm_request_duration_seconds: Histogram of LLM request latencies
- active_connections: Gauge of current HTTP connections
- active_agent_runs: Gauge of concurrent agent executions
- token_budget_remaining: Gauge of remaining token budget per tenant

Design:
- Uses prometheus_client library for metrics collection
- Middleware captures HTTP request metrics automatically
- Manual instrumentation for LLM and agent metrics
- Thread-safe counters/histograms/gauges
- Integrates with existing MetricsCollector for dual export
"""

from __future__ import annotations

import time
from collections.abc import Callable

import structlog
from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware

log = structlog.get_logger(__name__)


# Create custom registry to avoid conflicts with other prometheus exporters
REGISTRY = CollectorRegistry(auto_describe=True)


# ------------------------------------------------------------------ #
# HTTP Metrics
# ------------------------------------------------------------------ #

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=REGISTRY,
)

active_connections = Gauge(
    "active_connections",
    "Number of active HTTP connections",
    registry=REGISTRY,
)


# ------------------------------------------------------------------ #
# LLM Metrics
# ------------------------------------------------------------------ #

llm_requests_total = Counter(
    "llm_requests_total",
    "Total LLM API requests",
    ["model", "status"],
    registry=REGISTRY,
)

llm_request_duration_seconds = Histogram(
    "llm_request_duration_seconds",
    "LLM request latency in seconds",
    ["model"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
    registry=REGISTRY,
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "Total tokens consumed",
    ["model", "token_type"],
    registry=REGISTRY,
)


# ------------------------------------------------------------------ #
# Agent Metrics
# ------------------------------------------------------------------ #

active_agent_runs = Gauge(
    "active_agent_runs",
    "Number of concurrent agent executions",
    registry=REGISTRY,
)

agent_run_duration_seconds = Histogram(
    "agent_run_duration_seconds",
    "Agent execution duration in seconds",
    ["agent_type", "status"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
    registry=REGISTRY,
)

agent_steps_total = Counter(
    "agent_steps_total",
    "Total agent steps executed",
    ["agent_type"],
    registry=REGISTRY,
)


# ------------------------------------------------------------------ #
# Token Budget Metrics
# ------------------------------------------------------------------ #

token_budget_remaining = Gauge(
    "token_budget_remaining",
    "Remaining token budget",
    ["tenant_id", "period"],
    registry=REGISTRY,
)

token_budget_used_total = Counter(
    "token_budget_used_total",
    "Total tokens used against budget",
    ["tenant_id", "period"],
    registry=REGISTRY,
)


# ------------------------------------------------------------------ #
# Tool Metrics
# ------------------------------------------------------------------ #

tool_calls_total = Counter(
    "tool_calls_total",
    "Total tool invocations",
    ["tool_name", "success"],
    registry=REGISTRY,
)

tool_duration_seconds = Histogram(
    "tool_duration_seconds",
    "Tool execution duration in seconds",
    ["tool_name"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=REGISTRY,
)


# ------------------------------------------------------------------ #
# Instrumentation Functions
# ------------------------------------------------------------------ #


def record_http_request(
    method: str,
    endpoint: str,
    status_code: int,
    duration_seconds: float,
) -> None:
    """Record HTTP request metrics.

    Args:
        method: HTTP method
        endpoint: Request path
        status_code: Response status code
        duration_seconds: Request duration in seconds
    """
    http_requests_total.labels(
        method=method,
        endpoint=endpoint,
        status=str(status_code),
    ).inc()

    http_request_duration_seconds.labels(
        method=method,
        endpoint=endpoint,
    ).observe(duration_seconds)


def record_llm_request(
    model: str,
    status: str,
    duration_seconds: float,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Record LLM request metrics.

    Args:
        model: LLM model identifier
        status: Request status (success, error, timeout)
        duration_seconds: Request duration in seconds
        prompt_tokens: Number of prompt tokens
        completion_tokens: Number of completion tokens
    """
    llm_requests_total.labels(
        model=model,
        status=status,
    ).inc()

    llm_request_duration_seconds.labels(
        model=model,
    ).observe(duration_seconds)

    llm_tokens_total.labels(
        model=model,
        token_type="prompt",
    ).inc(prompt_tokens)

    llm_tokens_total.labels(
        model=model,
        token_type="completion",
    ).inc(completion_tokens)


def record_agent_run(
    agent_type: str,
    status: str,
    duration_seconds: float,
    steps: int,
) -> None:
    """Record agent run metrics.

    Args:
        agent_type: Agent type identifier
        status: Run status (success, error, timeout)
        duration_seconds: Run duration in seconds
        steps: Number of steps executed
    """
    agent_run_duration_seconds.labels(
        agent_type=agent_type,
        status=status,
    ).observe(duration_seconds)

    agent_steps_total.labels(
        agent_type=agent_type,
    ).inc(steps)


def record_tool_call(
    tool_name: str,
    success: bool,
    duration_seconds: float,
) -> None:
    """Record tool call metrics.

    Args:
        tool_name: Tool identifier
        success: Whether the call succeeded
        duration_seconds: Call duration in seconds
    """
    tool_calls_total.labels(
        tool_name=tool_name,
        success=str(success).lower(),
    ).inc()

    tool_duration_seconds.labels(
        tool_name=tool_name,
    ).observe(duration_seconds)


def update_token_budget(
    tenant_id: str,
    period: str,
    remaining: int,
    used: int,
) -> None:
    """Update token budget metrics.

    Args:
        tenant_id: Tenant identifier
        period: Budget period (daily, monthly)
        remaining: Remaining tokens in budget
        used: Tokens used since last update
    """
    token_budget_remaining.labels(
        tenant_id=tenant_id,
        period=period,
    ).set(remaining)

    if used > 0:
        token_budget_used_total.labels(
            tenant_id=tenant_id,
            period=period,
        ).inc(used)


# ------------------------------------------------------------------ #
# Middleware
# ------------------------------------------------------------------ #


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Middleware that records HTTP request metrics for Prometheus.

    Integrates with existing MetricsCollector to provide dual export.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Intercept request and record Prometheus metrics.

        Args:
            request: Incoming request
            call_next: Next middleware/handler

        Returns:
            Response
        """
        # Skip metrics for Prometheus endpoint itself
        if request.url.path == "/metrics":
            return await call_next(request)

        # Track active connections
        active_connections.inc()

        try:
            start_time = time.time()

            # Process request
            response = await call_next(request)

            # Calculate duration
            duration_seconds = time.time() - start_time

            # Record metrics
            record_http_request(
                method=request.method,
                endpoint=request.url.path,
                status_code=response.status_code,
                duration_seconds=duration_seconds,
            )

            return response

        except Exception:
            # Record error metrics
            duration_seconds = time.time() - start_time
            record_http_request(
                method=request.method,
                endpoint=request.url.path,
                status_code=500,
                duration_seconds=duration_seconds,
            )
            raise

        finally:
            # Decrement active connections
            active_connections.dec()


# ------------------------------------------------------------------ #
# Metrics Endpoint
# ------------------------------------------------------------------ #


def get_metrics() -> Response:
    """Generate Prometheus metrics in exposition format.

    Returns:
        Response with text/plain content type
    """
    metrics_output = generate_latest(REGISTRY)

    return Response(
        content=metrics_output,
        media_type=CONTENT_TYPE_LATEST,
        status_code=200,
    )
