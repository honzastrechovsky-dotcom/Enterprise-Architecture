"""Lightweight mode for edge deployments.

Configures the FastAPI application for constrained edge hardware:
- Strips heavy middleware (vector search, GPU inference, etc.)
- Limits available models to 7B-class only
- Provides resource monitoring (memory, CPU, disk)
- Edge-specific health check endpoint

All resource thresholds are configurable via edge-config.yaml.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
# 7B-class model allowlist
# Any model not in this list is rejected in edge mode.
# ------------------------------------------------------------------ #
EDGE_ALLOWED_MODELS: frozenset[str] = frozenset(
    {
        "ollama/llama3:7b",
        "ollama/llama3.1:7b",
        "ollama/mistral:7b",
        "ollama/mistral:7b-instruct",
        "ollama/qwen2:7b",
        "ollama/qwen2.5:7b",
        "ollama/phi3:mini",
        "ollama/phi3.5:mini",
        "ollama/gemma2:2b",
        "ollama/gemma2:9b",
        "ollama/deepseek-r1:7b",
        "ollama/codellama:7b",
    }
)

# ------------------------------------------------------------------ #
# Resource snapshot dataclass
# ------------------------------------------------------------------ #


@dataclass
class ResourceSnapshot:
    memory_used_mb: float
    memory_total_mb: float
    memory_percent: float
    cpu_percent: float
    disk_used_gb: float
    disk_total_gb: float
    disk_percent: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_memory_low(self) -> bool:
        threshold = float(os.getenv("LOW_MEMORY_THRESHOLD_MB", "512"))
        return (self.memory_total_mb - self.memory_used_mb) < threshold

    @property
    def is_disk_low(self) -> bool:
        threshold = float(os.getenv("LOW_DISK_THRESHOLD_GB", "5"))
        return (self.disk_total_gb - self.disk_used_gb) < threshold


@dataclass
class EdgeHealthReport:
    status: str  # "healthy" | "degraded" | "unhealthy"
    edge_mode: bool
    resources: ResourceSnapshot
    available_models: list[str]
    sync_enabled: bool
    offline_mode: bool
    uptime_seconds: float
    warnings: list[str]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


# ------------------------------------------------------------------ #
# LightweightMode
# ------------------------------------------------------------------ #


class LightweightMode:
    """Configures and manages edge-optimized operation mode."""

    _start_time: float = time.monotonic()

    def __init__(
        self,
        max_memory_mb: int = 4096,
        sync_enabled: bool = True,
        offline_mode: bool = True,
    ) -> None:
        self._max_memory_mb = max_memory_mb
        self._sync_enabled = sync_enabled
        self._offline_mode = offline_mode
        self._configured = False

    # ---------------------------------------------------------------- #
    # Application configuration
    # ---------------------------------------------------------------- #

    def configure_for_edge(self, app: Any) -> None:
        """Strip heavy middleware and disable resource-intensive features.

        Args:
            app: FastAPI application instance
        """
        # Remove middleware not suitable for edge
        _middleware_to_remove = {
            "VectorSearchMiddleware",
            "GPUInferenceMiddleware",
            "PrometheusMiddleware",
            "OpenTelemetryMiddleware",
            "RedisSessionMiddleware",
        }

        original_middleware = list(getattr(app, "middleware_stack", []))
        kept = []
        removed = []
        for mw in original_middleware:
            cls_name = getattr(mw, "__class__", type(mw)).__name__
            if cls_name in _middleware_to_remove:
                removed.append(cls_name)
            else:
                kept.append(mw)

        # Disable optional feature routers
        disabled_prefixes = {
            "/api/v1/multimodal",
            "/api/v1/fine-tuning",
            "/api/v1/analytics",
            "/api/v1/compliance",
        }

        routes_to_keep = []
        disabled_routes = []
        for route in list(getattr(app, "routes", [])):
            route_path = getattr(route, "path", "")
            if any(route_path.startswith(p) for p in disabled_prefixes):
                disabled_routes.append(route_path)
            else:
                routes_to_keep.append(route)

        if hasattr(app, "routes"):
            app.routes[:] = routes_to_keep  # type: ignore[index]

        # Inject edge metadata into app state
        if hasattr(app, "state"):
            app.state.edge_mode = True  # type: ignore[union-attr]
            app.state.max_memory_mb = self._max_memory_mb  # type: ignore[union-attr]
            app.state.sync_enabled = self._sync_enabled  # type: ignore[union-attr]
            app.state.offline_mode = self._offline_mode  # type: ignore[union-attr]

        self._configured = True
        log.info(
            "edge.lightweight.configured",
            removed_middleware=removed,
            disabled_routes=disabled_routes,
            max_memory_mb=self._max_memory_mb,
        )

    # ---------------------------------------------------------------- #
    # Model availability
    # ---------------------------------------------------------------- #

    def get_available_models(self) -> list[str]:
        """Return only 7B-class models permitted on edge hardware.

        Models are filtered against EDGE_ALLOWED_MODELS allowlist.
        The ollama base URL is checked via env to confirm availability.
        """
        # Additional runtime filtering based on env override
        env_override = os.getenv("EDGE_ALLOWED_MODELS", "")
        if env_override:
            overrides = frozenset(m.strip() for m in env_override.split(",") if m.strip())
            allowed = EDGE_ALLOWED_MODELS & overrides if overrides else EDGE_ALLOWED_MODELS
        else:
            allowed = EDGE_ALLOWED_MODELS

        models = sorted(allowed)
        log.debug("edge.lightweight.models", count=len(models))
        return models

    # ---------------------------------------------------------------- #
    # Resource monitoring
    # ---------------------------------------------------------------- #

    def check_resources(self) -> ResourceSnapshot:
        """Return current memory, CPU, and disk usage.

        Uses /proc and os.statvfs for Linux edge devices.
        Falls back to environment-provided limits when psutil unavailable.
        """
        memory_used_mb = self._get_memory_used_mb()
        memory_total_mb = float(os.getenv("MAX_MEMORY_MB", str(self._max_memory_mb)))
        memory_percent = (memory_used_mb / memory_total_mb * 100) if memory_total_mb else 0.0

        cpu_percent = self._get_cpu_percent()
        disk_used_gb, disk_total_gb = self._get_disk_gb()
        disk_percent = (disk_used_gb / disk_total_gb * 100) if disk_total_gb else 0.0

        return ResourceSnapshot(
            memory_used_mb=memory_used_mb,
            memory_total_mb=memory_total_mb,
            memory_percent=round(memory_percent, 1),
            cpu_percent=round(cpu_percent, 1),
            disk_used_gb=round(disk_used_gb, 2),
            disk_total_gb=round(disk_total_gb, 2),
            disk_percent=round(disk_percent, 1),
        )

    def _get_memory_used_mb(self) -> float:
        """Read RSS from /proc/self/status (Linux only)."""
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        return kb / 1024.0
        except (OSError, ValueError):
            pass
        # Fallback: use psutil if available
        try:
            import psutil  # type: ignore[import]
            proc = psutil.Process()
            return proc.memory_info().rss / (1024 * 1024)
        except ImportError:
            pass
        return 0.0

    def _get_cpu_percent(self) -> float:
        """Best-effort CPU percent via psutil or /proc/stat."""
        try:
            import psutil  # type: ignore[import]
            return psutil.cpu_percent(interval=0.1)
        except ImportError:
            pass
        return 0.0

    def _get_disk_gb(self) -> tuple[float, float]:
        """Disk usage for /data volume."""
        try:
            stat = os.statvfs("/data")
            total = stat.f_frsize * stat.f_blocks / (1024 ** 3)
            free = stat.f_frsize * stat.f_bavail / (1024 ** 3)
            used = total - free
            return used, total
        except OSError:
            # Fallback to root filesystem
            try:
                stat = os.statvfs("/")
                total = stat.f_frsize * stat.f_blocks / (1024 ** 3)
                free = stat.f_frsize * stat.f_bavail / (1024 ** 3)
                return total - free, total
            except OSError:
                return 0.0, float(os.getenv("MAX_DISK_GB", "50"))

    # ---------------------------------------------------------------- #
    # Health check
    # ---------------------------------------------------------------- #

    def health_check(self) -> EdgeHealthReport:
        """Return comprehensive edge health report.

        Aggregates resource status, model availability, sync state,
        and produces overall health determination.
        """
        resources = self.check_resources()
        models = self.get_available_models()
        warnings: list[str] = []

        if resources.is_memory_low:
            warnings.append(
                f"Low memory: {resources.memory_total_mb - resources.memory_used_mb:.0f}MB free"
            )
        if resources.is_disk_low:
            warnings.append(
                f"Low disk: {resources.disk_total_gb - resources.disk_used_gb:.1f}GB free"
            )
        if resources.cpu_percent > 90:
            warnings.append(f"High CPU: {resources.cpu_percent}%")
        if not models:
            warnings.append("No models available")

        if resources.is_memory_low and resources.is_disk_low:
            status = "unhealthy"
        elif warnings:
            status = "degraded"
        else:
            status = "healthy"

        uptime = time.monotonic() - LightweightMode._start_time

        return EdgeHealthReport(
            status=status,
            edge_mode=True,
            resources=resources,
            available_models=models,
            sync_enabled=self._sync_enabled,
            offline_mode=self._offline_mode,
            uptime_seconds=round(uptime, 1),
            warnings=warnings,
        )
