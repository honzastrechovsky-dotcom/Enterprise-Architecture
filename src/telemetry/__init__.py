"""Telemetry package for observability.

This package contains:
- Structured logging with trace correlation
- OpenTelemetry integration (in src/infra/telemetry.py)
- Prometheus metrics (in src/middleware/prometheus.py)
"""

from __future__ import annotations

from src.telemetry.logging import (
    RequestIdMiddleware,
    bind_agent_context,
    bind_tenant_context,
    bind_user_context,
    clear_context,
    configure_logging,
)

__all__ = [
    "RequestIdMiddleware",
    "bind_agent_context",
    "bind_tenant_context",
    "bind_user_context",
    "clear_context",
    "configure_logging",
]
