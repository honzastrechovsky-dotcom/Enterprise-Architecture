"""Metrics middleware for automatic API call tracking.

Captures all API requests and records metrics in a non-blocking way.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from src.services.metrics import MetricsCollector

log = structlog.get_logger(__name__)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware that captures API call metrics."""

    def __init__(self, app: ASGIApp) -> None:
        """Initialize middleware.

        Args:
            app: ASGI application
        """
        super().__init__(app)
        self.collector = MetricsCollector()

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Intercept request and record metrics.

        Args:
            request: Incoming request
            call_next: Next middleware/handler

        Returns:
            Response
        """
        # Skip metrics collection for health checks and docs
        if request.url.path in ["/health", "/ready", "/docs", "/redoc", "/openapi.json"]:
            return await call_next(request)

        start_time = time.time()

        # Process request
        response = await call_next(request)

        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)

        # Extract tenant and user from request state (set by AuthMiddleware)
        tenant_id = getattr(request.state, "tenant_id", None)
        user_id = getattr(request.state, "user_id", None)

        # Record metric (non-blocking)
        if tenant_id:
            try:
                await self.collector.record_api_call(
                    tenant_id=tenant_id,
                    endpoint=request.url.path,
                    method=request.method,
                    status_code=response.status_code,
                    response_time_ms=response_time_ms,
                    user_id=user_id,
                )
            except Exception as exc:
                # Don't fail the request if metrics collection fails
                log.warning(
                    "metrics.record_failed",
                    error=str(exc),
                    path=request.url.path,
                )

        return response
