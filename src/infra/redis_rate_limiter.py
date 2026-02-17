"""
Redis-backed distributed rate limiter using sliding window algorithm.

Drop-in replacement for the in-memory RateLimiter that works across
multiple API instances. Uses Redis sorted sets for efficient time-window
operations with atomic operations.

Key features:
- Sliding window (more accurate than fixed window)
- Per-user and per-tenant rate limiting
- Configurable burst allowance
- Rate limit headers (X-RateLimit-*)
- Graceful fallback to in-memory if Redis unavailable
- Connection pooling for performance

Algorithm:
- Store timestamps as sorted set scores
- Remove expired entries at each check (ZREMRANGEBYSCORE)
- Count entries in current window (ZCARD)
- Add current request (ZADD)
- All operations in Lua script for atomicity

Design decisions:
- Key format: rate_limit:{user_id}:{window_start_minute}
- TTL per key to prevent memory leak
- Lua script ensures atomic check-and-increment
- Falls back to in-memory limiter on Redis connection failure
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from fastapi import HTTPException, status

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = Any  # type: ignore

from src.core.rate_limit import RateLimiter as InMemoryRateLimiter

log = structlog.get_logger(__name__)


# Lua script for atomic rate limit check
# Uses sorted set with timestamps as scores
_LUA_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

-- Remove entries outside the sliding window
local window_start = now - window
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- Count current entries
local current = redis.call('ZCARD', key)

if current >= limit then
    return {0, current, limit}
end

-- Add current request
redis.call('ZADD', key, now, now)
redis.call('EXPIRE', key, ttl)

return {1, current + 1, limit}
"""


class RedisRateLimiter:
    """
    Redis-backed distributed rate limiter with sliding window algorithm.

    Supports per-user and per-tenant rate limiting with burst allowance.
    Falls back to in-memory rate limiting if Redis is unavailable.

    Example:
        limiter = RedisRateLimiter(
            redis_url="redis://localhost:6379/0",
            requests_per_minute=60,
            burst_allowance=10,
        )
        await limiter.connect()

        try:
            headers = await limiter.check(user_id="user-123")
            # Add headers to response: X-RateLimit-*
        except HTTPException as exc:
            # 429 Too Many Requests
            raise
    """

    def __init__(
        self,
        redis_url: str,
        requests_per_minute: int,
        *,
        burst_allowance: int = 0,
        window_seconds: int = 60,
    ) -> None:
        """
        Initialize Redis rate limiter.

        Args:
            redis_url: Redis connection URL
            requests_per_minute: Base rate limit per user
            burst_allowance: Extra requests allowed for bursts
            window_seconds: Sliding window duration (default 60)
        """
        if not REDIS_AVAILABLE:
            log.warning("redis_rate_limiter.redis_not_installed")
            self._fallback = InMemoryRateLimiter(requests_per_minute)
            self._redis: aioredis.Redis | None = None
            return

        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = None
        self._rpm = requests_per_minute
        self._burst = burst_allowance
        self._window = window_seconds
        self._limit = requests_per_minute + burst_allowance
        self._fallback = InMemoryRateLimiter(requests_per_minute)
        self._script_sha: str | None = None

        log.info(
            "redis_rate_limiter.initialized",
            rpm=requests_per_minute,
            burst=burst_allowance,
            window=window_seconds,
        )

    async def connect(self) -> None:
        """Establish Redis connection and load Lua script."""
        if not REDIS_AVAILABLE or self._redis is not None:
            return

        try:
            self._redis = await aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
                max_connections=10,
            )

            # Load Lua script and cache SHA
            self._script_sha = await self._redis.script_load(_LUA_RATE_LIMIT_SCRIPT)

            await self._redis.ping()
            log.info("redis_rate_limiter.connected", url=self._redis_url)

        except Exception as exc:
            log.error(
                "redis_rate_limiter.connect_failed",
                error=str(exc),
                fallback="in-memory",
            )
            self._redis = None

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            await self._redis.close()
            log.info("redis_rate_limiter.closed")

    async def check(
        self,
        user_id: uuid.UUID | str,
        *,
        tenant_id: uuid.UUID | str | None = None,
    ) -> dict[str, str]:
        """
        Check and increment rate limit for user.

        Returns headers dict for response:
            X-RateLimit-Limit: total limit
            X-RateLimit-Remaining: requests left in window
            X-RateLimit-Reset: Unix timestamp when window resets

        Raises HTTPException 429 if limit exceeded.

        Args:
            user_id: User identifier
            tenant_id: Optional tenant identifier for tenant-level limits
        """
        if self._rpm <= 0:
            # Unlimited
            return self._headers(limit=0, remaining=0, reset=int(time.time()) + 60)

        # Try Redis first
        if self._redis is not None:
            try:
                return await self._check_redis(user_id, tenant_id=tenant_id)
            except Exception as exc:
                log.error(
                    "redis_rate_limiter.check_failed",
                    user_id=str(user_id),
                    error=str(exc),
                    fallback="in-memory",
                )
                # Fall through to in-memory

        # Fallback to in-memory
        await self._fallback.check(user_id)
        # In-memory limiter doesn't return headers, so we estimate
        return self._headers(
            limit=self._limit,
            remaining=self._limit // 2,  # Estimate
            reset=int(time.time()) + 60,
        )

    async def _check_redis(
        self,
        user_id: uuid.UUID | str,
        *,
        tenant_id: uuid.UUID | str | None = None,
    ) -> dict[str, str]:
        """Execute rate limit check via Redis Lua script."""
        if self._redis is None or self._script_sha is None:
            raise RuntimeError("Redis not connected")

        key = self._make_key(user_id, tenant_id=tenant_id)
        now = time.time()
        ttl = self._window * 2  # Keep keys for 2x window duration

        # Execute Lua script atomically
        result = await self._redis.evalsha(
            self._script_sha,
            1,  # Number of keys
            key,
            now,
            self._window,
            self._limit,
            ttl,
        )

        allowed, current, limit = result

        if not allowed:
            # Rate limit exceeded
            retry_after = self._window
            log.warning(
                "rate_limit.exceeded",
                user_id=str(user_id),
                current=current,
                limit=limit,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {limit} requests per {self._window}s",
                headers={
                    "Retry-After": str(retry_after),
                    **self._headers(
                        limit=limit,
                        remaining=0,
                        reset=int(now) + retry_after,
                    ),
                },
            )

        remaining = limit - current
        reset = int(now) + self._window

        return self._headers(limit=limit, remaining=remaining, reset=reset)

    def _make_key(
        self,
        user_id: uuid.UUID | str,
        *,
        tenant_id: uuid.UUID | str | None = None,
    ) -> str:
        """
        Generate Redis key for rate limit tracking.

        Format: rate_limit:{tenant_id}:{user_id}
        If tenant_id is None: rate_limit:user:{user_id}
        """
        user_str = str(user_id)
        if tenant_id is None:
            return f"rate_limit:user:{user_str}"
        return f"rate_limit:{tenant_id}:{user_str}"

    def _headers(self, *, limit: int, remaining: int, reset: int) -> dict[str, str]:
        """Build rate limit response headers."""
        return {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(max(0, remaining)),
            "X-RateLimit-Reset": str(reset),
        }

    async def reset(self, user_id: uuid.UUID | str) -> None:
        """Reset rate limit for a user (useful in tests)."""
        if self._redis is not None:
            try:
                key = self._make_key(user_id)
                await self._redis.delete(key)
                log.info("redis_rate_limiter.reset", user_id=str(user_id))
            except Exception as exc:
                log.error(
                    "redis_rate_limiter.reset_failed",
                    user_id=str(user_id),
                    error=str(exc),
                )
        else:
            self._fallback.reset(user_id)
