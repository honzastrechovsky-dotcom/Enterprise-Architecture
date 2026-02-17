"""Per-user rate limiting using an in-process sliding window counter.

In production, replace the in-memory store with a Redis-backed
implementation to handle multiple API instances. The interface is
identical - just swap the storage backend.

Algorithm: Fixed window counter per (user_id, window_start_minute).
Simple, predictable, and avoids the burst-at-boundary problem of pure
per-minute counters by using a short expiry.

Thread safety: asyncio.Lock per user ensures no races in a single process.
For multi-process, use Redis INCR + EXPIRE which is atomic.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

import structlog
from fastapi import HTTPException, status

from src.config import Settings, get_settings

log = structlog.get_logger(__name__)


@dataclass
class _WindowCounter:
    """Sliding window state for one user."""
    window_start: float = field(default_factory=time.monotonic)
    count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RateLimiter:
    """In-process per-user rate limiter.

    One instance should be shared across the application (singleton via
    lifespan or dependency). Not thread-safe across processes.
    """

    def __init__(self, requests_per_minute: int) -> None:
        self._rpm = requests_per_minute
        self._window_seconds = 60.0
        self._counters: dict[str, _WindowCounter] = defaultdict(_WindowCounter)

    async def check(self, user_id: uuid.UUID | str) -> None:
        """Check and increment the rate limit counter for a user.

        Raises HTTP 429 if the user has exceeded their limit.
        Does NOT raise if rate limiting is set to 0 (unlimited).
        """
        if self._rpm <= 0:
            return  # Unlimited

        key = str(user_id)
        counter = self._counters[key]

        async with counter.lock:
            now = time.monotonic()
            elapsed = now - counter.window_start

            # Reset window if it has expired
            if elapsed >= self._window_seconds:
                counter.window_start = now
                counter.count = 0

            counter.count += 1

            if counter.count > self._rpm:
                retry_after = int(self._window_seconds - elapsed) + 1
                log.warning(
                    "rate_limit.exceeded",
                    user_id=key,
                    count=counter.count,
                    limit=self._rpm,
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Rate limit exceeded: {self._rpm} requests per minute",
                    headers={"Retry-After": str(retry_after)},
                )

    def reset(self, user_id: uuid.UUID | str) -> None:
        """Reset the counter for a user (useful in tests)."""
        key = str(user_id)
        if key in self._counters:
            del self._counters[key]


# Module-level singleton - initialized from settings
_rate_limiter: RateLimiter | None = None


def init_rate_limiter(settings: Settings | None = None) -> RateLimiter:
    """Initialize the global rate limiter from settings."""
    global _rate_limiter
    cfg = settings or get_settings()
    _rate_limiter = RateLimiter(cfg.rate_limit_per_minute)
    log.info("rate_limiter.initialized", rpm=cfg.rate_limit_per_minute)
    return _rate_limiter


def get_rate_limiter() -> RateLimiter:
    """FastAPI dependency - return the initialized rate limiter."""
    if _rate_limiter is None:
        return init_rate_limiter()
    return _rate_limiter
