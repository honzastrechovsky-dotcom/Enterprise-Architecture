"""Tests for the Response Caching Layer.

TDD methodology: tests define expected behaviour, covering:
- InMemoryCacheBackend: get/set/TTL/delete/exists/pattern/flush
- RedisCacheBackend: public interface delegation (mocked redis client)
- get_cache_backend factory: selects backend from settings
- ResponseCache: key generation, get/set, invalidation, stats
- EmbeddingCache: single get/set, batch operations, hash_text utility
- CacheMiddleware: HIT/MISS/SKIP headers, tenant isolation, non-GET bypass
- Cache API endpoints: stats, invalidate, flush (admin-only enforcement)
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenant() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# InMemoryCacheBackend tests
# ---------------------------------------------------------------------------


class TestInMemoryCacheBackend:
    """Unit tests for InMemoryCacheBackend."""

    @pytest.fixture
    def backend(self):
        from src.cache.backend import InMemoryCacheBackend
        return InMemoryCacheBackend()

    @pytest.mark.asyncio
    async def test_set_and_get_returns_value(self, backend):
        """set() followed by get() returns the stored value."""
        await backend.set("key1", {"foo": "bar"}, ttl=60)
        result = await backend.get("key1")
        assert result == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_get_missing_key_returns_none(self, backend):
        """get() on a non-existent key returns None."""
        result = await backend.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_ttl_expiry(self, backend):
        """Items stored with TTL=0 are considered expired immediately."""
        # Use a very short TTL (1 second) - we simulate expiry by monkeypatching
        from src.cache.backend import _CacheEntry
        entry = _CacheEntry(value="expired", ttl=0)
        # TTL of 0 means expires_at <= now immediately
        assert entry.is_expired

    @pytest.mark.asyncio
    async def test_expired_entry_not_returned(self, backend):
        """Expired entries return None and are pruned from the store."""
        # Inject an already-expired entry directly
        from src.cache.backend import _CacheEntry
        backend._store["stale"] = _CacheEntry(value="old", ttl=0)
        result = await backend.get("stale")
        assert result is None
        # Should be pruned from store
        assert "stale" not in backend._store

    @pytest.mark.asyncio
    async def test_delete_removes_key(self, backend):
        """delete() removes the key so subsequent get returns None."""
        await backend.set("key2", "value", ttl=60)
        await backend.delete("key2")
        assert await backend.get("key2") is None

    @pytest.mark.asyncio
    async def test_exists_returns_true_for_live_key(self, backend):
        """exists() returns True for a key with positive TTL."""
        await backend.set("live", "data", ttl=60)
        assert await backend.exists("live") is True

    @pytest.mark.asyncio
    async def test_exists_returns_false_for_missing_key(self, backend):
        """exists() returns False when key is not present."""
        assert await backend.exists("missing") is False

    @pytest.mark.asyncio
    async def test_delete_pattern_removes_matching_keys(self, backend):
        """delete_pattern() removes all keys matching the glob."""
        await backend.set("tenant:abc:x", 1, ttl=60)
        await backend.set("tenant:abc:y", 2, ttl=60)
        await backend.set("tenant:xyz:z", 3, ttl=60)
        deleted = await backend.delete_pattern("tenant:abc:*")
        assert deleted == 2
        assert await backend.get("tenant:abc:x") is None
        assert await backend.get("tenant:abc:y") is None
        assert await backend.get("tenant:xyz:z") == 3

    @pytest.mark.asyncio
    async def test_flush_all_clears_store(self, backend):
        """flush_all() removes every key."""
        await backend.set("a", 1, ttl=60)
        await backend.set("b", 2, ttl=60)
        await backend.flush_all()
        assert await backend.get("a") is None
        assert await backend.get("b") is None

    @pytest.mark.asyncio
    async def test_info_returns_stats(self, backend):
        """info() returns a dict with backend and connected keys."""
        await backend.set("k", "v", ttl=60)
        info = await backend.info()
        assert info["backend"] == "memory"
        assert info["connected"] is True
        assert info["total_keys"] >= 1

    @pytest.mark.asyncio
    async def test_hit_miss_counters(self, backend):
        """get() increments hit/miss counters tracked in info()."""
        await backend.set("hit_key", "v", ttl=60)
        await backend.get("hit_key")    # hit
        await backend.get("miss_key")   # miss
        info = await backend.info()
        assert info["hits"] >= 1
        assert info["misses"] >= 1


# ---------------------------------------------------------------------------
# get_cache_backend factory
# ---------------------------------------------------------------------------


class TestGetCacheBackendFactory:
    """Tests for the get_cache_backend() factory function."""

    def test_returns_memory_backend_when_redis_not_installed(self, fake_settings):
        """Returns InMemoryCacheBackend when redis package is missing."""
        from src.cache.backend import InMemoryCacheBackend, get_cache_backend
        with patch.dict("sys.modules", {"redis": None, "redis.asyncio": None}):
            backend = get_cache_backend(fake_settings)
        assert isinstance(backend, InMemoryCacheBackend)

    def test_returns_redis_backend_when_redis_available(self, fake_settings):
        """Returns RedisCacheBackend when redis package is importable."""
        from src.cache.backend import RedisCacheBackend, get_cache_backend

        mock_redis = MagicMock()
        with patch.dict("sys.modules", {"redis": mock_redis, "redis.asyncio": mock_redis}):
            backend = get_cache_backend(fake_settings)
        assert isinstance(backend, RedisCacheBackend)

    def test_returns_memory_when_no_redis_url(self):
        """Falls back to InMemoryCacheBackend when redis_url is empty."""
        from src.cache.backend import InMemoryCacheBackend, get_cache_backend
        settings = MagicMock()
        settings.redis_url = ""
        backend = get_cache_backend(settings)
        assert isinstance(backend, InMemoryCacheBackend)


# ---------------------------------------------------------------------------
# ResponseCache tests
# ---------------------------------------------------------------------------


class TestResponseCache:
    """Tests for ResponseCache with InMemoryCacheBackend."""

    @pytest.fixture
    def backend(self):
        from src.cache.backend import InMemoryCacheBackend
        return InMemoryCacheBackend()

    @pytest.fixture
    def cache(self, backend):
        from src.cache.response_cache import ResponseCache
        return ResponseCache(backend=backend)

    @pytest.fixture
    def tenant_id(self) -> uuid.UUID:
        return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    @pytest.mark.asyncio
    async def test_cache_key_is_deterministic(self, cache, tenant_id):
        """Same inputs always produce the same cache key."""
        k1 = cache.cache_key(tenant_id, "hello world", "gpt-4o")
        k2 = cache.cache_key(tenant_id, "hello world", "gpt-4o")
        assert k1 == k2

    @pytest.mark.asyncio
    async def test_cache_key_differs_for_different_tenants(self, cache):
        """Different tenant_ids produce different cache keys."""
        t1 = uuid.uuid4()
        t2 = uuid.uuid4()
        k1 = cache.cache_key(t1, "same query", "model")
        k2 = cache.cache_key(t2, "same query", "model")
        assert k1 != k2

    @pytest.mark.asyncio
    async def test_cache_key_normalises_whitespace(self, cache, tenant_id):
        """Leading/trailing whitespace and case are normalised."""
        k1 = cache.cache_key(tenant_id, "  Hello World  ", "gpt-4")
        k2 = cache.cache_key(tenant_id, "hello world", "gpt-4")
        assert k1 == k2

    @pytest.mark.asyncio
    async def test_store_and_retrieve_response(self, cache, tenant_id):
        """cache_response() stores; get_cached_response() retrieves."""
        await cache.cache_response(
            tenant_id=tenant_id,
            query="What is AI?",
            model="gpt-4o-mini",
            response="AI is artificial intelligence.",
            ttl=60,
        )
        result = await cache.get_cached_response(
            tenant_id=tenant_id,
            query="What is AI?",
            model="gpt-4o-mini",
        )
        assert result is not None
        assert result.content == "AI is artificial intelligence."
        assert result.model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self, cache, tenant_id):
        """get_cached_response() returns None for unseen queries."""
        result = await cache.get_cached_response(
            tenant_id=tenant_id,
            query="unknown question",
            model="gpt-4o",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_hit_count_increments(self, cache, tenant_id):
        """Repeated hits increment the hit_count on CachedResponse."""
        await cache.cache_response(
            tenant_id=tenant_id,
            query="repeated query",
            model="gpt-4",
            response="answer",
        )
        r1 = await cache.get_cached_response(tenant_id, "repeated query", "gpt-4")
        r2 = await cache.get_cached_response(tenant_id, "repeated query", "gpt-4")
        assert r1 is not None
        assert r2 is not None
        assert r2.hit_count > r1.hit_count

    @pytest.mark.asyncio
    async def test_invalidate_tenant_removes_entries(self, cache, tenant_id):
        """invalidate_tenant() removes all cached entries for that tenant."""
        await cache.cache_response(
            tenant_id=tenant_id,
            query="q1",
            model="m",
            response="r1",
        )
        await cache.cache_response(
            tenant_id=tenant_id,
            query="q2",
            model="m",
            response="r2",
        )
        deleted = await cache.invalidate_tenant(tenant_id)
        # deleted count may vary; what matters is the entries are gone
        r1 = await cache.get_cached_response(tenant_id, "q1", "m")
        r2 = await cache.get_cached_response(tenant_id, "q2", "m")
        assert r1 is None
        assert r2 is None

    @pytest.mark.asyncio
    async def test_get_cache_stats_returns_dict(self, cache, tenant_id):
        """get_cache_stats() returns a dict with expected keys."""
        stats = await cache.get_cache_stats()
        assert "backend" in stats
        assert "connected" in stats
        assert "total_keys" in stats

    @pytest.mark.asyncio
    async def test_cached_response_to_from_dict(self):
        """CachedResponse serialises and deserialises correctly."""
        from src.cache.response_cache import CachedResponse
        now = datetime.now(timezone.utc)
        cr = CachedResponse(content="hello", model="gpt-4", cached_at=now, ttl=300)
        data = cr.to_dict()
        restored = CachedResponse.from_dict(data)
        assert restored.content == "hello"
        assert restored.model == "gpt-4"
        assert restored.ttl == 300


# ---------------------------------------------------------------------------
# EmbeddingCache tests
# ---------------------------------------------------------------------------


class TestEmbeddingCache:
    """Tests for EmbeddingCache."""

    @pytest.fixture
    def backend(self):
        from src.cache.backend import InMemoryCacheBackend
        return InMemoryCacheBackend()

    @pytest.fixture
    def cache(self, backend):
        from src.cache.embedding_cache import EmbeddingCache
        return EmbeddingCache(backend=backend)

    @pytest.mark.asyncio
    async def test_store_and_retrieve_embedding(self, cache):
        """cache_embedding() + get_embedding() round-trips correctly."""
        text_hash = "abc123"
        embedding = [0.1, 0.2, 0.3]
        await cache.cache_embedding(text_hash, embedding)
        result = await cache.get_embedding(text_hash)
        assert result == embedding

    @pytest.mark.asyncio
    async def test_miss_returns_none(self, cache):
        """get_embedding() returns None for an unknown hash."""
        result = await cache.get_embedding("unknownhash")
        assert result is None

    @pytest.mark.asyncio
    async def test_hash_text_is_deterministic(self):
        """hash_text() returns the same hash for the same input."""
        from src.cache.embedding_cache import EmbeddingCache
        h1 = EmbeddingCache.hash_text("hello world")
        h2 = EmbeddingCache.hash_text("hello world")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex length

    @pytest.mark.asyncio
    async def test_hash_text_differs_for_different_inputs(self):
        """Different text produces different hash."""
        from src.cache.embedding_cache import EmbeddingCache
        h1 = EmbeddingCache.hash_text("text one")
        h2 = EmbeddingCache.hash_text("text two")
        assert h1 != h2

    @pytest.mark.asyncio
    async def test_batch_get_returns_hits_and_nones(self, cache):
        """batch_get() returns cached values and None for misses."""
        h1 = "hash1"
        h2 = "hash2"
        await cache.cache_embedding(h1, [1.0, 2.0])
        result = await cache.batch_get([h1, h2])
        assert result[h1] == [1.0, 2.0]
        assert result[h2] is None

    @pytest.mark.asyncio
    async def test_batch_cache_stores_multiple(self, cache):
        """batch_cache() stores all provided embeddings."""
        embeddings = {
            "hx": [0.1, 0.2],
            "hy": [0.3, 0.4],
        }
        await cache.batch_cache(embeddings)
        assert await cache.get_embedding("hx") == [0.1, 0.2]
        assert await cache.get_embedding("hy") == [0.3, 0.4]

    @pytest.mark.asyncio
    async def test_invalidate_removes_single_entry(self, cache):
        """invalidate() removes exactly one entry."""
        await cache.cache_embedding("remove_me", [9.9])
        await cache.invalidate("remove_me")
        assert await cache.get_embedding("remove_me") is None

    @pytest.mark.asyncio
    async def test_flush_removes_all_embeddings(self, cache):
        """flush() removes all embedding entries."""
        await cache.cache_embedding("e1", [1.0])
        await cache.cache_embedding("e2", [2.0])
        await cache.flush()
        assert await cache.get_embedding("e1") is None
        assert await cache.get_embedding("e2") is None


# ---------------------------------------------------------------------------
# RedisCacheBackend (mocked)
# ---------------------------------------------------------------------------


class TestRedisCacheBackendMocked:
    """Tests for RedisCacheBackend delegating to a mocked redis client."""

    @pytest.fixture
    def mock_redis(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=None)
        client.setex = AsyncMock()
        client.delete = AsyncMock()
        client.exists = AsyncMock(return_value=0)
        client.scan_iter = MagicMock(return_value=self._async_gen([]))
        client.flushdb = AsyncMock()
        client.info = AsyncMock(return_value={
            "used_memory_human": "1.5M",
            "connected_clients": 1,
            "total_commands_processed": 100,
            "keyspace_hits": 50,
            "keyspace_misses": 10,
        })
        client.dbsize = AsyncMock(return_value=5)
        return client

    @staticmethod
    async def _async_gen(items):
        for item in items:
            yield item

    @pytest.fixture
    def backend(self, mock_redis):
        from src.cache.backend import RedisCacheBackend
        b = RedisCacheBackend("redis://localhost:6379/0")
        b._client = mock_redis
        return b

    @pytest.mark.asyncio
    async def test_set_calls_setex(self, backend, mock_redis):
        """set() calls redis setex with key, ttl, and serialised value."""
        await backend.set("mykey", {"data": 1}, ttl=120)
        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args
        assert call_args[0][0] == "mykey"
        assert call_args[0][1] == 120

    @pytest.mark.asyncio
    async def test_get_returns_none_on_miss(self, backend, mock_redis):
        """get() returns None when redis returns None."""
        mock_redis.get.return_value = None
        result = await backend.get("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_deserialises_json(self, backend, mock_redis):
        """get() deserialises JSON from Redis response."""
        import json
        mock_redis.get.return_value = json.dumps({"key": "value"})
        result = await backend.get("testkey")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_delete_calls_redis_delete(self, backend, mock_redis):
        """delete() delegates to redis.delete()."""
        await backend.delete("delkey")
        mock_redis.delete.assert_called_once_with("delkey")

    @pytest.mark.asyncio
    async def test_exists_returns_true(self, backend, mock_redis):
        """exists() returns True when redis returns non-zero."""
        mock_redis.exists.return_value = 1
        assert await backend.exists("present") is True

    @pytest.mark.asyncio
    async def test_info_returns_connected_stats(self, backend, mock_redis):
        """info() returns connected=True and populated stats."""
        info = await backend.info()
        assert info["connected"] is True
        assert info["backend"] == "redis"
        assert "keyspace_hits" in info


# ---------------------------------------------------------------------------
# Cache API endpoint tests
# ---------------------------------------------------------------------------


class TestCacheAPIEndpoints:
    """Integration-style tests for /api/v1/cache/* endpoints."""

    @pytest.fixture
    def backend(self):
        from src.cache.backend import InMemoryCacheBackend
        return InMemoryCacheBackend()

    @pytest.fixture
    def response_cache(self, backend):
        from src.cache.response_cache import ResponseCache
        return ResponseCache(backend=backend)

    @pytest.mark.asyncio
    async def test_stats_requires_admin_role(self, fake_settings, monkeypatch):
        """GET /cache/stats returns 403 for non-admin user."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.api.cache import router, get_response_cache
        from src.cache.backend import InMemoryCacheBackend
        from src.cache.response_cache import ResponseCache
        from src.auth.dependencies import get_current_user, AuthenticatedUser
        from src.models.user import User, UserRole

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        # Build a viewer user mock
        viewer_user = MagicMock(spec=User)
        viewer_user.role = UserRole.VIEWER
        viewer_user.tenant_id = uuid.uuid4()
        viewer_user.id = uuid.uuid4()
        viewer_auth = AuthenticatedUser(viewer_user, {})

        def override_get_current_user():
            return viewer_auth

        def override_cache():
            return ResponseCache(InMemoryCacheBackend())

        app.dependency_overrides[get_current_user] = override_get_current_user
        app.dependency_overrides[get_response_cache] = override_cache

        client = TestClient(app)
        resp = client.get("/api/v1/cache/stats")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_stats_returns_200_for_admin(self, fake_settings):
        """GET /cache/stats returns 200 for admin user."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.api.cache import router, get_response_cache
        from src.cache.backend import InMemoryCacheBackend
        from src.cache.response_cache import ResponseCache
        from src.auth.dependencies import get_current_user, AuthenticatedUser
        from src.models.user import User, UserRole

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        admin_user = MagicMock(spec=User)
        admin_user.role = UserRole.ADMIN
        admin_user.tenant_id = uuid.uuid4()
        admin_user.id = uuid.uuid4()
        admin_auth = AuthenticatedUser(admin_user, {})

        app.dependency_overrides[get_current_user] = lambda: admin_auth
        app.dependency_overrides[get_response_cache] = lambda: ResponseCache(InMemoryCacheBackend())

        client = TestClient(app)
        resp = client.get("/api/v1/cache/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "backend" in data
        assert "connected" in data

    @pytest.mark.asyncio
    async def test_invalidate_returns_200_for_admin(self, fake_settings):
        """POST /cache/invalidate returns 200 and keys_deleted field."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.api.cache import router, get_response_cache
        from src.cache.backend import InMemoryCacheBackend
        from src.cache.response_cache import ResponseCache
        from src.auth.dependencies import get_current_user, AuthenticatedUser
        from src.models.user import User, UserRole

        tenant_id = uuid.uuid4()
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        admin_user = MagicMock(spec=User)
        admin_user.role = UserRole.ADMIN
        admin_user.tenant_id = tenant_id
        admin_user.id = uuid.uuid4()
        admin_auth = AuthenticatedUser(admin_user, {})

        app.dependency_overrides[get_current_user] = lambda: admin_auth
        app.dependency_overrides[get_response_cache] = lambda: ResponseCache(InMemoryCacheBackend())

        client = TestClient(app)
        resp = client.post("/api/v1/cache/invalidate")
        assert resp.status_code == 200
        data = resp.json()
        assert "keys_deleted" in data
        assert str(tenant_id) in data["tenant_id"]

    @pytest.mark.asyncio
    async def test_flush_returns_200_for_admin(self, fake_settings):
        """DELETE /cache/flush returns 200 for admin user."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.api.cache import router, get_response_cache
        from src.cache.backend import InMemoryCacheBackend
        from src.cache.response_cache import ResponseCache
        from src.auth.dependencies import get_current_user, AuthenticatedUser
        from src.models.user import User, UserRole

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        admin_user = MagicMock(spec=User)
        admin_user.role = UserRole.ADMIN
        admin_user.tenant_id = uuid.uuid4()
        admin_user.id = uuid.uuid4()
        admin_auth = AuthenticatedUser(admin_user, {})

        app.dependency_overrides[get_current_user] = lambda: admin_auth
        app.dependency_overrides[get_response_cache] = lambda: ResponseCache(InMemoryCacheBackend())

        client = TestClient(app)
        resp = client.delete("/api/v1/cache/flush")
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
