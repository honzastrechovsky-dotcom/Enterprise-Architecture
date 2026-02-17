"""FastAPI application entrypoint.

Application startup order:
1. Load settings (from environment)
2. Initialize database engine and session factory
3. Initialize rate limiter
4. Register middleware (auth, CORS, logging)
5. Include all routers

Shutdown order:
1. Close DB connection pool
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.router import api_v1_router, public_router
from src.auth.middleware import AuthMiddleware
from src.config import get_settings
from src.core.rate_limit import init_rate_limiter
from src.core.security import (
    RequestIdMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from src.database import close_db, init_db
from src.infra.background_worker import BackgroundWorkerPool
from src.infra.health import HealthCheckRouter
from src.infra.telemetry import TracingMiddleware, instrument_fastapi, setup_telemetry
from src.middleware.metrics import MetricsMiddleware
from src.middleware.prometheus import PrometheusMiddleware, get_metrics
from src.telemetry.logging import configure_logging
from src.websocket.chat import ws_router as websocket_router
from src.websocket.manager import get_connection_manager

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown."""
    settings = get_settings()

    # Configure structured logging first (before any log calls)
    configure_logging(
        json_logs=settings.is_prod,
        log_level="DEBUG" if settings.debug else "INFO",
    )

    log.info(
        "app.starting",
        environment=settings.environment,
        db_url=settings.database_url.split("@")[-1],
    )

    # Initialize infrastructure
    init_db(settings)
    init_rate_limiter(settings)

    # Initialize telemetry and observability
    setup_telemetry(settings)
    instrument_fastapi(app)  # Instrument FastAPI with OpenTelemetry

    # Initialize background workers
    worker_pool = BackgroundWorkerPool(pool_size=4)
    await worker_pool.start()

    # Initialize metrics collector with DB session
    from src.database import get_db_session
    from src.services.metrics import MetricsCollector

    async for db in get_db_session():
        collector = MetricsCollector()
        await collector.initialize(db)
        break  # Just need to initialize once

    # Store collector in app state so shutdown can reference the same instance
    app.state.metrics_collector = collector

    # Store worker pool in app state for access in endpoints
    app.state.worker_pool = worker_pool

    # Initialize WebSocket ConnectionManager
    ws_manager = get_connection_manager()
    app.state.ws_manager = ws_manager
    log.info("app.ws_manager_initialized")

    log.info("app.ready")
    yield

    # WebSocket cleanup (connections will be dropped on process exit;
    # log the final count for observability)
    log.info("app.ws_manager_shutdown", active_connections=ws_manager.connection_count())

    # Shutdown: cleanup background workers, telemetry, and metrics collector
    # Use the same collector instance stored during startup (not a new singleton)
    await app.state.metrics_collector.shutdown()

    await worker_pool.stop()
    await close_db()
    log.info("app.shutdown")


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()

    app = FastAPI(
        title="Enterprise Agent Platform",
        description=(
            "Multi-tenant enterprise agent platform with RAG, audit logging, "
            "and OIDC authentication."
        ),
        version="0.1.0",
        docs_url="/docs" if settings.is_dev else None,
        redoc_url="/redoc" if settings.is_dev else None,
        openapi_url="/openapi.json" if settings.is_dev else None,
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------ #
    # Middleware (added in reverse order - last added = first executed)
    # ------------------------------------------------------------------ #

    # CORS (must be first in execution order, so add last)
    # In dev mode, allow all origins for easier development
    # In production, restrict to configured allowed origins
    cors_origins = ["*"] if settings.is_dev else settings.cors_allowed_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=settings.is_prod,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )
    # Distributed tracing (OpenTelemetry)
    app.add_middleware(TracingMiddleware)

    # Prometheus metrics
    app.add_middleware(PrometheusMiddleware)

    # DB-backed metrics collection
    app.add_middleware(MetricsMiddleware)

    # Security headers (CSP, HSTS, etc. per OWASP guidelines)
    app.add_middleware(SecurityHeadersMiddleware, is_production=settings.is_prod)

    # Request size limit (10 MB default, prevents DoS via large payloads)
    app.add_middleware(RequestSizeLimitMiddleware, max_size=10 * 1024 * 1024)

    # Unique request ID for log correlation and audit trails
    app.add_middleware(RequestIdMiddleware)

    # JWT extraction and validation
    app.add_middleware(AuthMiddleware)

    # ------------------------------------------------------------------ #
    # Routers
    # ------------------------------------------------------------------ #
    app.include_router(public_router)
    app.include_router(api_v1_router)

    # WebSocket routes (mounted directly - not under api_v1_router
    # because WebSocket endpoints don't benefit from HTTP middleware the same way)
    app.include_router(websocket_router)

    # Health check endpoints
    health_router = HealthCheckRouter(settings)
    app.include_router(health_router)

    # Prometheus metrics endpoint
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Any:
        """Prometheus metrics endpoint."""
        return get_metrics()

    # ------------------------------------------------------------------ #
    # Global exception handlers
    # ------------------------------------------------------------------ #

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "app.unhandled_exception",
            path=request.url.path,
            method=request.method,
            error=str(exc),
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return app


# Module-level app instance for uvicorn
app = create_app()
