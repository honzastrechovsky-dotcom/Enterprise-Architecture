"""Connector result caching with tenant isolation.

ConnectorCache provides a simple in-memory LRU cache for connector results:
- 5-minute TTL per cache entry
- Tenant-isolated (cache keys include tenant_id)
- LRU eviction with max 1000 entries per tenant
- Cache hit/miss metrics via structlog

This is an in-memory LRU implementation. For distributed deployments, replace with a Redis-backed cache.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Cache TTL in seconds
DEFAULT_TTL_SECONDS = 300  # 5 minutes

# Max cache entries per tenant (LRU eviction)
MAX_ENTRIES_PER_TENANT = 1000


@dataclass
class CacheEntry:
    """A single cache entry with TTL."""

    value: Any
    expires_at: float  # Unix timestamp

    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        return time.time() > self.expires_at


class ConnectorCache:
    """In-memory LRU cache with tenant isolation and TTL.

    Cache keys are hashed from (tenant_id, connector, operation, params).
    Each tenant has independent LRU eviction.

    Thread-safe for async use (Python GIL protects dict operations).
    """

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries_per_tenant: int = MAX_ENTRIES_PER_TENANT,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries_per_tenant = max_entries_per_tenant
        # Nested dict: tenant_id -> OrderedDict[cache_key, CacheEntry]
        self._cache: dict[str, OrderedDict[str, CacheEntry]] = {}

    def _get_tenant_cache(self, tenant_id: uuid.UUID) -> OrderedDict[str, CacheEntry]:
        """Get or create the cache OrderedDict for a tenant."""
        tenant_key = str(tenant_id)
        if tenant_key not in self._cache:
            self._cache[tenant_key] = OrderedDict()
        return self._cache[tenant_key]

    def _make_cache_key(
        self,
        connector: str,
        operation: str,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> str:
        """Generate a stable cache key from operation parameters.

        Includes user_id so different users get independent caches
        (future: user-level cache if needed).

        Returns a SHA256 hex digest for compact keys.
        """
        key_parts = {
            "connector": connector,
            "operation": operation,
            "user_id": str(user_id),
            "params": params,
        }
        # Stable JSON serialization
        key_json = json.dumps(key_parts, sort_keys=True)
        return hashlib.sha256(key_json.encode()).hexdigest()

    def get(
        self,
        tenant_id: uuid.UUID,
        connector: str,
        operation: str,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> Any | None:
        """Retrieve cached result if present and not expired.

        Returns:
            Cached value or None if not found/expired
        """
        tenant_cache = self._get_tenant_cache(tenant_id)
        cache_key = self._make_cache_key(connector, operation, user_id, params)

        entry = tenant_cache.get(cache_key)
        if entry is None:
            log.debug(
                "connector.cache_miss",
                tenant_id=str(tenant_id),
                connector=connector,
                operation=operation,
                reason="not_found",
            )
            return None

        if entry.is_expired():
            # Remove expired entry
            del tenant_cache[cache_key]
            log.debug(
                "connector.cache_miss",
                tenant_id=str(tenant_id),
                connector=connector,
                operation=operation,
                reason="expired",
            )
            return None

        # Move to end (most recently used)
        tenant_cache.move_to_end(cache_key)

        log.debug(
            "connector.cache_hit",
            tenant_id=str(tenant_id),
            connector=connector,
            operation=operation,
        )

        return entry.value

    def set(
        self,
        tenant_id: uuid.UUID,
        connector: str,
        operation: str,
        user_id: uuid.UUID,
        params: dict[str, Any],
        value: Any,
    ) -> None:
        """Store a value in the cache with TTL.

        If the tenant cache exceeds max_entries_per_tenant, evict LRU entry.
        """
        tenant_cache = self._get_tenant_cache(tenant_id)
        cache_key = self._make_cache_key(connector, operation, user_id, params)

        # LRU eviction if at capacity
        if len(tenant_cache) >= self.max_entries_per_tenant:
            # Remove oldest entry (first item in OrderedDict)
            evicted_key, _ = tenant_cache.popitem(last=False)
            log.debug(
                "connector.cache_evict",
                tenant_id=str(tenant_id),
                reason="lru_full",
            )

        # Insert new entry
        expires_at = time.time() + self.ttl_seconds
        tenant_cache[cache_key] = CacheEntry(value=value, expires_at=expires_at)

        log.debug(
            "connector.cache_set",
            tenant_id=str(tenant_id),
            connector=connector,
            operation=operation,
            ttl_seconds=self.ttl_seconds,
        )

    def invalidate_tenant(self, tenant_id: uuid.UUID) -> None:
        """Invalidate all cache entries for a tenant.

        Useful for admin operations or tenant deletion.
        """
        tenant_key = str(tenant_id)
        if tenant_key in self._cache:
            count = len(self._cache[tenant_key])
            del self._cache[tenant_key]
            log.info(
                "connector.cache_invalidate_tenant",
                tenant_id=tenant_key,
                entries_cleared=count,
            )

    def invalidate_all(self) -> None:
        """Clear the entire cache (all tenants).

        Used for testing or emergency cache flush.
        """
        total_entries = sum(len(cache) for cache in self._cache.values())
        self._cache.clear()
        log.info("connector.cache_invalidate_all", entries_cleared=total_entries)

    def get_stats(self, tenant_id: uuid.UUID | None = None) -> dict[str, Any]:
        """Get cache statistics.

        Args:
            tenant_id: If provided, stats for that tenant. Otherwise global stats.

        Returns:
            Dict with cache statistics
        """
        if tenant_id:
            tenant_cache = self._get_tenant_cache(tenant_id)
            return {
                "tenant_id": str(tenant_id),
                "entry_count": len(tenant_cache),
                "max_entries": self.max_entries_per_tenant,
            }

        # Global stats
        total_entries = sum(len(cache) for cache in self._cache.values())
        return {
            "tenant_count": len(self._cache),
            "total_entries": total_entries,
            "max_entries_per_tenant": self.max_entries_per_tenant,
        }
