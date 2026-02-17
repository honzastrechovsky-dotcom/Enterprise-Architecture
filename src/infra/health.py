"""
Enhanced health checks for Kubernetes and production monitoring.

Includes FastAPI router for health check endpoints.

Provides detailed component-level health status beyond basic liveness.
Implements Kubernetes-compatible endpoints for liveness and readiness probes.

Endpoints:
- /healthz: Liveness probe (is the process running?)
- /readyz: Readiness probe (can the service handle requests?)

Components checked:
- database: PostgreSQL connection and query execution
- redis: Redis connection (if configured)
- llm_proxy: LiteLLM proxy availability
- disk_space: Available disk space for temp files

Design:
- Each component check has timeout and error handling
- Checks run in parallel for speed
- Structured JSON response with component details
- Degrades gracefully (some components can be degraded/warning)

Health status levels:
- healthy: All systems operational
- degraded: Some non-critical components failing
- unhealthy: Critical components failing (DB, LLM proxy)

Example response:
    {
        "status": "healthy",
        "timestamp": "2026-02-16T12:34:56Z",
        "components": {
            "database": {"status": "healthy", "latency_ms": 5.2},
            "redis": {"status": "healthy", "latency_ms": 1.1},
            "llm_proxy": {"status": "healthy", "latency_ms": 150.3},
            "disk_space": {"status": "healthy", "free_gb": 45.2}
        }
    }
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.database import get_engine

log = structlog.get_logger(__name__)


class ComponentStatus(StrEnum):
    """Health status for individual components."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ComponentHealth:
    """Health check result for a single component."""
    status: ComponentStatus
    latency_ms: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class SystemHealth:
    """Overall system health status."""
    status: ComponentStatus
    timestamp: str
    components: dict[str, ComponentHealth]

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "status": self.status,
            "timestamp": self.timestamp,
            "components": {
                name: {
                    "status": comp.status,
                    "latency_ms": comp.latency_ms,
                    "details": comp.details,
                    "error": comp.error,
                }
                for name, comp in self.components.items()
            },
        }


class HealthCheck:
    """
    Orchestrates health checks for all system components.

    Runs checks in parallel with timeouts and error handling.
    Provides Kubernetes-compatible liveness and readiness endpoints.

    Example:
        health = HealthCheck(settings)
        result = await health.check_all()

        if result.status == ComponentStatus.HEALTHY:
            return 200, result.to_dict()
        elif result.status == ComponentStatus.DEGRADED:
            return 200, result.to_dict()  # Still serve traffic
        else:
            return 503, result.to_dict()  # Service unavailable
    """

    def __init__(
        self,
        settings: Settings,
        *,
        check_timeout: float = 5.0,
    ) -> None:
        """
        Initialize health checker.

        Args:
            settings: Application settings
            check_timeout: Timeout per component check (seconds)
        """
        self._settings = settings
        self._check_timeout = check_timeout

    async def check_all(self) -> SystemHealth:
        """
        Run all component health checks in parallel.

        Returns overall system health with component details.
        """
        start = time.perf_counter()

        # Run checks concurrently
        results = await asyncio.gather(
            self._check_database(),
            self._check_redis(),
            self._check_llm_proxy(),
            self._check_disk_space(),
            return_exceptions=True,
        )

        # Unpack results (handle exceptions)
        db_health = results[0] if not isinstance(results[0], Exception) else self._error_health(results[0])
        redis_health = results[1] if not isinstance(results[1], Exception) else self._error_health(results[1])
        llm_health = results[2] if not isinstance(results[2], Exception) else self._error_health(results[2])
        disk_health = results[3] if not isinstance(results[3], Exception) else self._error_health(results[3])

        components = {
            "database": db_health,
            "redis": redis_health,
            "llm_proxy": llm_health,
            "disk_space": disk_health,
        }

        # Determine overall status
        overall = self._aggregate_status(components)

        duration_ms = (time.perf_counter() - start) * 1000

        log.info(
            "health_check.completed",
            status=overall,
            duration_ms=round(duration_ms, 2),
            components={k: v.status for k, v in components.items()},
        )

        return SystemHealth(
            status=overall,
            timestamp=datetime.now(UTC).isoformat(),
            components=components,
        )

    async def check_liveness(self) -> bool:
        """
        Liveness probe: Is the process alive?

        Returns True if the process is running and can respond.
        This is a lightweight check - does not test external dependencies.
        """
        # Basic sanity check - can we access settings?
        try:
            _ = self._settings.environment
            return True
        except Exception as exc:
            log.error("health_check.liveness_failed", error=str(exc))
            return False

    async def check_readiness(self) -> bool:
        """
        Readiness probe: Can the service handle requests?

        Returns True if critical components (DB, LLM proxy) are healthy.
        """
        try:
            result = await self.check_all()

            # Critical components must be healthy
            db_ok = result.components["database"].status == ComponentStatus.HEALTHY
            llm_ok = result.components["llm_proxy"].status in (
                ComponentStatus.HEALTHY,
                ComponentStatus.DEGRADED,  # LLM can be slow but still usable
            )

            return db_ok and llm_ok

        except Exception as exc:
            log.error("health_check.readiness_failed", error=str(exc))
            return False

    async def _check_database(self) -> ComponentHealth:
        """Check PostgreSQL connection and query execution."""
        start = time.perf_counter()

        try:
            engine = get_engine()

            async with AsyncSession(engine) as session:
                # Execute simple query
                result = await asyncio.wait_for(
                    session.execute(text("SELECT 1")),
                    timeout=self._check_timeout,
                )
                _ = result.scalar()

            latency_ms = (time.perf_counter() - start) * 1000

            return ComponentHealth(
                status=ComponentStatus.HEALTHY,
                latency_ms=round(latency_ms, 2),
                details={"query": "SELECT 1"},
            )

        except TimeoutError:
            return ComponentHealth(
                status=ComponentStatus.UNHEALTHY,
                error="Database query timeout",
            )
        except Exception as exc:
            log.error("health_check.database_failed", error=str(exc))
            return ComponentHealth(
                status=ComponentStatus.UNHEALTHY,
                error=str(exc),
            )

    async def _check_redis(self) -> ComponentHealth:
        """Check Redis connection and PING command."""
        start = time.perf_counter()

        # Skip if Redis not configured
        if not self._settings.redis_url or self._settings.redis_url == "redis://localhost:6379/0":
            return ComponentHealth(
                status=ComponentStatus.UNKNOWN,
                details={"message": "Redis not configured"},
            )

        try:
            import redis.asyncio as aioredis

            client = await aioredis.from_url(
                self._settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )

            try:
                await asyncio.wait_for(
                    client.ping(),
                    timeout=self._check_timeout,
                )

                latency_ms = (time.perf_counter() - start) * 1000

                return ComponentHealth(
                    status=ComponentStatus.HEALTHY,
                    latency_ms=round(latency_ms, 2),
                )

            finally:
                await client.close()

        except ModuleNotFoundError:
            return ComponentHealth(
                status=ComponentStatus.UNKNOWN,
                details={"message": "redis package not installed"},
            )
        except TimeoutError:
            return ComponentHealth(
                status=ComponentStatus.DEGRADED,
                error="Redis PING timeout",
            )
        except Exception as exc:
            log.warning("health_check.redis_failed", error=str(exc))
            return ComponentHealth(
                status=ComponentStatus.DEGRADED,  # Redis is non-critical
                error=str(exc),
            )

    async def _check_llm_proxy(self) -> ComponentHealth:
        """Check LiteLLM proxy availability."""
        start = time.perf_counter()

        try:
            url = f"{self._settings.litellm_base_url}/health"

            async with httpx.AsyncClient() as client:
                response = await asyncio.wait_for(
                    client.get(url, timeout=self._check_timeout),
                    timeout=self._check_timeout,
                )

            latency_ms = (time.perf_counter() - start) * 1000

            if response.status_code == 200:
                return ComponentHealth(
                    status=ComponentStatus.HEALTHY,
                    latency_ms=round(latency_ms, 2),
                    details={"url": url},
                )
            else:
                return ComponentHealth(
                    status=ComponentStatus.UNHEALTHY,
                    error=f"LLM proxy returned {response.status_code}",
                    details={"url": url},
                )

        except TimeoutError:
            return ComponentHealth(
                status=ComponentStatus.UNHEALTHY,
                error="LLM proxy health check timeout",
            )
        except Exception as exc:
            log.error("health_check.llm_proxy_failed", error=str(exc))
            return ComponentHealth(
                status=ComponentStatus.UNHEALTHY,
                error=str(exc),
            )

    async def _check_disk_space(self) -> ComponentHealth:
        """Check available disk space for temp files."""
        try:
            usage = shutil.disk_usage("/tmp")
            free_gb = usage.free / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            percent_free = (usage.free / usage.total) * 100

            # Warn if less than 10% free or less than 5GB
            if percent_free < 10 or free_gb < 5:
                status = ComponentStatus.DEGRADED
            else:
                status = ComponentStatus.HEALTHY

            return ComponentHealth(
                status=status,
                details={
                    "free_gb": round(free_gb, 2),
                    "total_gb": round(total_gb, 2),
                    "percent_free": round(percent_free, 2),
                },
            )

        except Exception as exc:
            log.error("health_check.disk_space_failed", error=str(exc))
            return ComponentHealth(
                status=ComponentStatus.UNKNOWN,
                error=str(exc),
            )

    def _error_health(self, exception: Exception) -> ComponentHealth:
        """Convert exception to unhealthy component health."""
        return ComponentHealth(
            status=ComponentStatus.UNHEALTHY,
            error=str(exception),
        )

    def _aggregate_status(self, components: dict[str, ComponentHealth]) -> ComponentStatus:
        """
        Determine overall status from component statuses.

        Rules:
        - If any critical component (DB, LLM) is unhealthy → UNHEALTHY
        - If any component is degraded → DEGRADED
        - Otherwise → HEALTHY
        """
        critical_components = ["database", "llm_proxy"]

        # Check critical components
        for name in critical_components:
            comp = components.get(name)
            if comp and comp.status == ComponentStatus.UNHEALTHY:
                return ComponentStatus.UNHEALTHY

        # Check for any degraded
        if any(c.status == ComponentStatus.DEGRADED for c in components.values()):
            return ComponentStatus.DEGRADED

        # All healthy or unknown
        return ComponentStatus.HEALTHY


# ------------------------------------------------------------------ #
# FastAPI Router
# ------------------------------------------------------------------ #


def HealthCheckRouter(settings: Settings | None = None) -> APIRouter:
    """Create health check router with endpoints.

    Args:
        settings: Application settings (will use get_settings if None)

    Returns:
        Configured APIRouter with health endpoints
    """
    router = APIRouter(tags=["health"])

    if settings is None:
        settings = get_settings()

    checker = HealthCheck(settings)

    @router.get("/health/live", status_code=200)
    async def liveness() -> dict[str, str]:
        """Liveness probe: Is the process alive?

        Returns 200 if the process is running.
        Kubernetes uses this to know if it should restart the pod.
        """
        is_alive = await checker.check_liveness()
        if is_alive:
            return {"status": "alive"}
        else:
            return JSONResponse(
                status_code=503,
                content={"status": "dead"},
            )

    @router.get("/health/ready", status_code=200)
    async def readiness() -> dict[str, str]:
        """Readiness probe: Can the service handle requests?

        Returns 200 if critical components (DB, LLM) are healthy.
        Kubernetes uses this to know if it should send traffic to this pod.
        """
        is_ready = await checker.check_readiness()
        if is_ready:
            return {"status": "ready"}
        else:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready"},
            )

    @router.get("/health", status_code=200)
    async def detailed_health() -> JSONResponse:
        """Detailed health check with component status.

        Returns overall system health plus status of each component.
        Use this for monitoring dashboards and debugging.
        """
        result = await checker.check_all()

        status_code = 200
        if result.status == ComponentStatus.UNHEALTHY:
            status_code = 503
        elif result.status == ComponentStatus.DEGRADED:
            status_code = 200  # Still serve traffic when degraded

        return JSONResponse(
            status_code=status_code,
            content=result.to_dict(),
        )

    return router
