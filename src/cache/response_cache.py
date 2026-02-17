"""Response cache - tenant-scoped LLM response caching.

Caches full LLM responses keyed on (tenant_id, query, model) so that
identical queries within a tenant never hit the LLM twice. Cache keys
use SHA-256 of the normalised inputs to guarantee determinism while
hiding raw query text from the key namespace.

Cache is per-tenant but NOT per-user: two different users in the same
tenant asking identical queries will share the cached response. This is
intentional and safe for the current access model (all users in a tenant
share the same document corpus and model configuration).

TTL design:
- Default TTL: 3600 s (1 hour). Callers may override.
- Agent-specific TTL may be shorter / longer as needed.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from src.cache.backend import CacheBackend

log = structlog.get_logger(__name__)

# Namespace prefix to avoid collisions with other cache users
_RESPONSE_NS = "resp"
# Namespace used when tracking hit counts per-key
_HITCOUNT_NS = "resp_hits"


@dataclass
class CachedResponse:
    """Represents a cached LLM response."""

    content: str
    model: str
    cached_at: datetime
    ttl: int
    hit_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "model": self.model,
            "cached_at": self.cached_at.isoformat(),
            "ttl": self.ttl,
            "hit_count": self.hit_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CachedResponse:
        return cls(
            content=data["content"],
            model=data["model"],
            cached_at=datetime.fromisoformat(data["cached_at"]),
            ttl=data["ttl"],
            hit_count=data.get("hit_count", 0),
        )


class ResponseCache:
    """Tenant-scoped response cache.

    All public methods are async to allow the underlying backend to be
    I/O-bound (Redis). The class itself holds no mutable state beyond the
    injected backend.
    """

    def __init__(self, backend: CacheBackend) -> None:
        self._backend = backend

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def cache_key(
        self,
        tenant_id: uuid.UUID,
        query: str,
        model: str,
        agent_id: uuid.UUID | None = None,
    ) -> str:
        """Build a deterministic, collision-resistant cache key.

        The key is a SHA-256 hash of the normalised inputs. Normalisation
        (strip + lower) prevents trivial cache misses from whitespace
        or casing differences. The tenant_id is mixed into the hash to
        guarantee isolation even if two tenants send identical queries.

        Returns:
            String of the form "resp:<hex_digest>"
        """
        normalised_query = query.strip().lower()
        normalised_model = model.strip().lower()
        agent_part = str(agent_id) if agent_id else ""
        raw = f"{tenant_id}:{normalised_query}:{normalised_model}:{agent_part}"
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"{_RESPONSE_NS}:{digest}"

    def _hitcount_key(self, cache_key: str) -> str:
        """Separate key for incrementing hit counts without re-serialising the response."""
        # Strip the namespace prefix and re-prefix with hitcount namespace
        suffix = cache_key.split(":", 1)[1] if ":" in cache_key else cache_key
        return f"{_HITCOUNT_NS}:{suffix}"

    def _tenant_pattern(self, tenant_id: uuid.UUID) -> str:
        """Glob pattern matching all response keys for a tenant.

        Because the tenant_id is baked into the hash we cannot reconstruct
        which specific keys belong to a tenant without storing an index.
        We therefore maintain a tenant-scoped index: a Redis set (or
        in-memory set) that records every key written for that tenant.
        The index key itself follows this pattern so it can be cleaned up.
        """
        return f"resp_idx:{tenant_id}:*"

    def _tenant_index_key(self, tenant_id: uuid.UUID, cache_key: str) -> str:
        """Key for tenant index entry that maps tenant -> cache_key."""
        return f"resp_idx:{tenant_id}:{cache_key}"

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def get_cached_response(
        self,
        tenant_id: uuid.UUID,
        query: str,
        model: str,
        agent_id: uuid.UUID | None = None,
    ) -> CachedResponse | None:
        """Return cached response if present, otherwise None.

        Also increments the hit_count counter for observability.
        """
        key = self.cache_key(tenant_id, query, model, agent_id)
        data = await self._backend.get(key)
        if data is None:
            log.debug("cache.response.miss", key=key)
            return None

        response = CachedResponse.from_dict(data)

        # Increment persistent hit counter (best-effort, non-blocking)
        hk = self._hitcount_key(key)
        hit_raw = await self._backend.get(hk)
        new_count = (hit_raw or 0) + 1
        response.hit_count = new_count
        # Persist incremented count with same TTL as cached response
        await self._backend.set(hk, new_count, response.ttl)

        log.debug("cache.response.hit", key=key, hit_count=new_count)
        return response

    async def cache_response(
        self,
        tenant_id: uuid.UUID,
        query: str,
        model: str,
        response: str,
        ttl: int = 3600,
        agent_id: uuid.UUID | None = None,
    ) -> str:
        """Store an LLM response in the cache.

        Args:
            tenant_id: Tenant that owns this response
            query: The original user query
            model: The model that generated the response
            response: The LLM response text
            ttl: Seconds until the cached response expires (default 1h)
            agent_id: Optional agent identity for agent-specific caching

        Returns:
            The cache key under which the response was stored.
        """
        key = self.cache_key(tenant_id, query, model, agent_id)
        now = datetime.now(UTC)
        cached = CachedResponse(
            content=response,
            model=model,
            cached_at=now,
            ttl=ttl,
            hit_count=0,
        )
        await self._backend.set(key, cached.to_dict(), ttl)

        # Write a sentinel into the tenant index so we can invalidate by tenant
        idx_key = self._tenant_index_key(tenant_id, key)
        await self._backend.set(idx_key, key, ttl)

        log.debug("cache.response.stored", key=key, ttl=ttl)
        return key

    async def invalidate_tenant(self, tenant_id: uuid.UUID) -> int:
        """Clear all cached responses for a tenant.

        The implementation uses a two-step approach:
        1. Find all tenant index sentinel keys (resp_idx:<tenant_id>:*)
        2. For each sentinel, delete the actual response key it points to
        3. Delete the sentinel keys themselves

        Returns the total number of cache-related keys removed.
        """
        pattern = self._tenant_pattern(tenant_id)

        # Collect all sentinel keys matching the tenant pattern
        # We use delete_pattern on a local InMemory backend which returns count,
        # but for Redis we need to first scan the sentinels to get the actual
        # response keys they point to.
        #
        # Strategy: iterate sentinel keys, read each to get the actual resp: key,
        # delete the actual resp: key + its hit-count key, then delete the sentinel.
        deleted = 0

        # For InMemoryCacheBackend we can scan _store directly
        from src.cache.backend import InMemoryCacheBackend
        if isinstance(self._backend, InMemoryCacheBackend):
            import fnmatch
            async with self._backend._lock:
                sentinel_keys = [
                    k for k in list(self._backend._store.keys())
                    if fnmatch.fnmatch(k, pattern)
                ]

            for sentinel_key in sentinel_keys:
                # The sentinel value IS the actual response key
                resp_key = await self._backend.get(sentinel_key)
                if resp_key:
                    await self._backend.delete(resp_key)
                    await self._backend.delete(self._hitcount_key(resp_key))
                    deleted += 1
                await self._backend.delete(sentinel_key)
                deleted += 1  # count the sentinel itself too
        else:
            # For Redis and other backends: use SCAN on the pattern,
            # retrieve each sentinel's value, then delete actual key + sentinel
            from src.cache.backend import RedisCacheBackend
            if isinstance(self._backend, RedisCacheBackend):
                try:
                    client = await self._backend._get_client()
                    async for sentinel_key in client.scan_iter(match=pattern, count=100):
                        resp_key = await self._backend.get(sentinel_key)
                        if resp_key:
                            await client.delete(resp_key)
                            await client.delete(self._hitcount_key(resp_key))
                            deleted += 1
                        await client.delete(sentinel_key)
                        deleted += 1
                except Exception as exc:
                    log.warning("cache.response.invalidate_failed", error=str(exc))
            else:
                # Generic fallback: just delete the pattern (sentinels only)
                deleted = await self._backend.delete_pattern(pattern)

        log.info(
            "cache.response.tenant_invalidated",
            tenant_id=str(tenant_id),
            keys_deleted=deleted,
        )
        return deleted

    async def invalidate_key(self, key: str) -> None:
        """Remove a single cache key (and its hit counter)."""
        await self._backend.delete(key)
        await self._backend.delete(self._hitcount_key(key))

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_cache_stats(self) -> dict[str, Any]:
        """Return cache statistics from the underlying backend."""
        info = await self._backend.info()
        return {
            "backend": info.get("backend", "unknown"),
            "connected": info.get("connected", False),
            "total_keys": info.get("total_keys", info.get("db_size", 0)),
            "hits": info.get("hits", info.get("keyspace_hits", 0)),
            "misses": info.get("misses", info.get("keyspace_misses", 0)),
            "hit_rate": info.get("hit_rate", 0.0),
            "used_memory_human": info.get("used_memory_human", "n/a"),
            "extra": info,
        }
