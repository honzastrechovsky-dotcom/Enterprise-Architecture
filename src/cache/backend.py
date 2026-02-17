"""Cache backend implementations.

Defines the CacheBackend ABC and two concrete implementations:
- RedisCacheBackend: Production backend using Redis with JSON serialization
- InMemoryCacheBackend: Dict-based backend with TTL, for testing/dev

The factory function get_cache_backend() selects the appropriate backend
based on settings. Redis is preferred; InMemory is the safe fallback so
the app works fine without a Redis connection.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class CacheBackend(ABC):
    """Abstract interface all cache backends must implement."""

    @abstractmethod
    async def get(self, key: str) -> Any | None:
        """Return cached value for key, or None if not found / expired."""

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: int) -> None:
        """Store value under key with TTL in seconds."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete key from cache (no-op if key does not exist)."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Return True if key exists and has not expired."""

    @abstractmethod
    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching a glob pattern. Returns deleted count."""

    @abstractmethod
    async def flush_all(self) -> None:
        """Remove ALL keys from the cache. Use with caution."""

    @abstractmethod
    async def info(self) -> dict[str, Any]:
        """Return backend-specific info/stats dict."""


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------


class RedisCacheBackend(CacheBackend):
    """Production cache backend backed by Redis.

    Uses aioredis (redis-py async) for all operations. Values are
    JSON-serialised so they round-trip cleanly without pickle security
    risks. Initialised lazily on first call so import never blocks.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Any = None  # redis.asyncio.Redis, set on first use

    async def _get_client(self) -> Any:
        """Return or create the Redis client (lazy init)."""
        if self._client is None:
            try:
                import redis.asyncio as aioredis  # type: ignore[import-untyped]
                self._client = aioredis.from_url(
                    self._redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "redis package is required for RedisCacheBackend. "
                    "Install it with: pip install redis"
                ) from exc
        return self._client

    async def get(self, key: str) -> Any | None:
        try:
            client = await self._get_client()
            raw = await client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            log.warning("cache.redis.get_failed", key=key, error=str(exc))
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            client = await self._get_client()
            serialised = json.dumps(value, default=str)
            await client.setex(key, ttl, serialised)
        except Exception as exc:
            log.warning("cache.redis.set_failed", key=key, error=str(exc))

    async def delete(self, key: str) -> None:
        try:
            client = await self._get_client()
            await client.delete(key)
        except Exception as exc:
            log.warning("cache.redis.delete_failed", key=key, error=str(exc))

    async def exists(self, key: str) -> bool:
        try:
            client = await self._get_client()
            result = await client.exists(key)
            return bool(result)
        except Exception as exc:
            log.warning("cache.redis.exists_failed", key=key, error=str(exc))
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching a glob pattern using SCAN + DEL."""
        try:
            client = await self._get_client()
            deleted = 0
            async for key in client.scan_iter(match=pattern, count=100):
                await client.delete(key)
                deleted += 1
            log.debug("cache.redis.pattern_deleted", pattern=pattern, deleted=deleted)
            return deleted
        except Exception as exc:
            log.warning("cache.redis.delete_pattern_failed", pattern=pattern, error=str(exc))
            return 0

    async def flush_all(self) -> None:
        try:
            client = await self._get_client()
            await client.flushdb()
            log.info("cache.redis.flushed_all")
        except Exception as exc:
            log.warning("cache.redis.flush_all_failed", error=str(exc))

    async def info(self) -> dict[str, Any]:
        try:
            client = await self._get_client()
            redis_info = await client.info()
            dbsize = await client.dbsize()
            return {
                "backend": "redis",
                "url": self._redis_url,
                "connected": True,
                "used_memory_human": redis_info.get("used_memory_human", "unknown"),
                "connected_clients": redis_info.get("connected_clients", 0),
                "total_commands_processed": redis_info.get("total_commands_processed", 0),
                "keyspace_hits": redis_info.get("keyspace_hits", 0),
                "keyspace_misses": redis_info.get("keyspace_misses", 0),
                "db_size": dbsize,
            }
        except Exception as exc:
            return {
                "backend": "redis",
                "url": self._redis_url,
                "connected": False,
                "error": str(exc),
            }

    async def close(self) -> None:
        """Close the Redis connection pool."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


# ---------------------------------------------------------------------------
# In-memory backend (testing / dev fallback)
# ---------------------------------------------------------------------------


class _CacheEntry:
    """Single entry stored by InMemoryCacheBackend."""

    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: int) -> None:
        self.value = value
        self.expires_at: float = time.monotonic() + ttl

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at


class InMemoryCacheBackend(CacheBackend):
    """Dict-backed cache with TTL support.

    Thread-safe via asyncio.Lock. Suitable for testing and single-process
    dev environments. Does NOT persist across process restarts.
    """

    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()
        # Simple hit/miss counters for stats
        self._hits: int = 0
        self._misses: int = 0

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None or entry.is_expired:
                if entry is not None and entry.is_expired:
                    del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            return entry.value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        async with self._lock:
            self._store[key] = _CacheEntry(value, ttl)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def exists(self, key: str) -> bool:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None or entry.is_expired:
                if entry is not None:
                    del self._store[key]
                return False
            return True

    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching a glob pattern (fnmatch semantics)."""
        import fnmatch

        async with self._lock:
            to_delete = [k for k in self._store if fnmatch.fnmatch(k, pattern)]
            for k in to_delete:
                del self._store[k]
            return len(to_delete)

    async def flush_all(self) -> None:
        async with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
        log.info("cache.memory.flushed_all")

    async def info(self) -> dict[str, Any]:
        async with self._lock:
            # Prune expired before counting
            expired = [k for k, v in self._store.items() if v.is_expired]
            for k in expired:
                del self._store[k]

            total_requests = self._hits + self._misses
            hit_rate = self._hits / total_requests if total_requests > 0 else 0.0

            return {
                "backend": "memory",
                "connected": True,
                "total_keys": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
            }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_cache_backend(settings: Any) -> CacheBackend:
    """Return the appropriate CacheBackend for the given settings.

    Tries to use Redis when the redis package is available and a redis_url
    is configured. Falls back to InMemoryCacheBackend so the application
    works even without Redis installed.

    Args:
        settings: Application Settings instance.

    Returns:
        A CacheBackend implementation ready for use.
    """
    redis_url: str = getattr(settings, "redis_url", "")

    if redis_url:
        try:
            import redis.asyncio  # noqa: F401  - just check importability
            log.info("cache.backend_selected", backend="redis", url=redis_url)
            return RedisCacheBackend(redis_url)
        except ImportError:
            log.warning(
                "cache.redis_unavailable",
                reason="redis package not installed - falling back to in-memory cache",
            )

    log.info("cache.backend_selected", backend="memory")
    return InMemoryCacheBackend()
