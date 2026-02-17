"""Tests for ConnectorCache."""

from __future__ import annotations

import time
import uuid

import pytest

from src.connectors.cache import ConnectorCache


class TestConnectorCache:
    """Tests for tenant-isolated LRU cache."""

    @pytest.fixture
    def cache(self) -> ConnectorCache:
        """Fixture providing cache instance with short TTL for testing."""
        return ConnectorCache(ttl_seconds=2, max_entries_per_tenant=3)

    @pytest.fixture
    def tenant_id(self) -> uuid.UUID:
        """Fixture providing test tenant ID."""
        return uuid.uuid4()

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        """Fixture providing test user ID."""
        return uuid.uuid4()

    def test_cache_miss_on_first_access(
        self,
        cache: ConnectorCache,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Test that cache returns None on first access."""
        result = cache.get(
            tenant_id=tenant_id,
            connector="test_connector",
            operation="test_op",
            user_id=user_id,
            params={"key": "value"},
        )
        assert result is None

    def test_cache_hit_after_set(
        self,
        cache: ConnectorCache,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Test that cache returns value after set."""
        test_value = {"data": "test"}

        cache.set(
            tenant_id=tenant_id,
            connector="test_connector",
            operation="test_op",
            user_id=user_id,
            params={"key": "value"},
            value=test_value,
        )

        result = cache.get(
            tenant_id=tenant_id,
            connector="test_connector",
            operation="test_op",
            user_id=user_id,
            params={"key": "value"},
        )

        assert result == test_value

    def test_cache_miss_after_ttl_expiry(
        self,
        cache: ConnectorCache,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Test that cache returns None after TTL expires."""
        cache.set(
            tenant_id=tenant_id,
            connector="test_connector",
            operation="test_op",
            user_id=user_id,
            params={"key": "value"},
            value={"data": "test"},
        )

        # Wait for TTL to expire (2 seconds + buffer)
        time.sleep(2.5)

        result = cache.get(
            tenant_id=tenant_id,
            connector="test_connector",
            operation="test_op",
            user_id=user_id,
            params={"key": "value"},
        )

        assert result is None

    def test_tenant_isolation(
        self,
        cache: ConnectorCache,
        user_id: uuid.UUID,
    ) -> None:
        """Test that tenants have isolated cache spaces."""
        tenant1 = uuid.uuid4()
        tenant2 = uuid.uuid4()

        # Set value for tenant 1
        cache.set(
            tenant_id=tenant1,
            connector="test",
            operation="op",
            user_id=user_id,
            params={},
            value={"tenant": "1"},
        )

        # Set different value for tenant 2
        cache.set(
            tenant_id=tenant2,
            connector="test",
            operation="op",
            user_id=user_id,
            params={},
            value={"tenant": "2"},
        )

        # Verify isolation
        result1 = cache.get(tenant1, "test", "op", user_id, {})
        result2 = cache.get(tenant2, "test", "op", user_id, {})

        assert result1 == {"tenant": "1"}
        assert result2 == {"tenant": "2"}

    def test_lru_eviction(
        self,
        cache: ConnectorCache,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Test that LRU eviction works when max entries exceeded."""
        # Cache configured with max_entries_per_tenant=3

        # Insert 3 entries
        for i in range(3):
            cache.set(
                tenant_id=tenant_id,
                connector="test",
                operation="op",
                user_id=user_id,
                params={"id": i},
                value=f"value_{i}",
            )

        # All 3 should be present
        assert cache.get(tenant_id, "test", "op", user_id, {"id": 0}) == "value_0"
        assert cache.get(tenant_id, "test", "op", user_id, {"id": 1}) == "value_1"
        assert cache.get(tenant_id, "test", "op", user_id, {"id": 2}) == "value_2"

        # Insert 4th entry - should evict oldest (id=0)
        cache.set(
            tenant_id=tenant_id,
            connector="test",
            operation="op",
            user_id=user_id,
            params={"id": 3},
            value="value_3",
        )

        # id=0 should be evicted
        assert cache.get(tenant_id, "test", "op", user_id, {"id": 0}) is None
        # Others should still be present
        assert cache.get(tenant_id, "test", "op", user_id, {"id": 1}) == "value_1"
        assert cache.get(tenant_id, "test", "op", user_id, {"id": 2}) == "value_2"
        assert cache.get(tenant_id, "test", "op", user_id, {"id": 3}) == "value_3"

    def test_different_params_create_different_keys(
        self,
        cache: ConnectorCache,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Test that different params create separate cache entries."""
        cache.set(
            tenant_id=tenant_id,
            connector="test",
            operation="op",
            user_id=user_id,
            params={"filter": "A"},
            value="result_A",
        )

        cache.set(
            tenant_id=tenant_id,
            connector="test",
            operation="op",
            user_id=user_id,
            params={"filter": "B"},
            value="result_B",
        )

        assert (
            cache.get(tenant_id, "test", "op", user_id, {"filter": "A"}) == "result_A"
        )
        assert (
            cache.get(tenant_id, "test", "op", user_id, {"filter": "B"}) == "result_B"
        )

    def test_invalidate_tenant(
        self,
        cache: ConnectorCache,
        user_id: uuid.UUID,
    ) -> None:
        """Test that invalidate_tenant clears all entries for that tenant."""
        tenant1 = uuid.uuid4()
        tenant2 = uuid.uuid4()

        # Add entries for both tenants
        cache.set(tenant1, "test", "op", user_id, {}, "value1")
        cache.set(tenant2, "test", "op", user_id, {}, "value2")

        # Invalidate tenant1
        cache.invalidate_tenant(tenant1)

        # Tenant1 should be cleared
        assert cache.get(tenant1, "test", "op", user_id, {}) is None
        # Tenant2 should remain
        assert cache.get(tenant2, "test", "op", user_id, {}) == "value2"

    def test_invalidate_all(
        self,
        cache: ConnectorCache,
        user_id: uuid.UUID,
    ) -> None:
        """Test that invalidate_all clears entire cache."""
        tenant1 = uuid.uuid4()
        tenant2 = uuid.uuid4()

        cache.set(tenant1, "test", "op", user_id, {}, "value1")
        cache.set(tenant2, "test", "op", user_id, {}, "value2")

        cache.invalidate_all()

        assert cache.get(tenant1, "test", "op", user_id, {}) is None
        assert cache.get(tenant2, "test", "op", user_id, {}) is None

    def test_get_stats_per_tenant(
        self,
        cache: ConnectorCache,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Test get_stats returns tenant-specific statistics."""
        # Add 2 entries
        cache.set(tenant_id, "test", "op1", user_id, {}, "value1")
        cache.set(tenant_id, "test", "op2", user_id, {}, "value2")

        stats = cache.get_stats(tenant_id)

        assert stats["tenant_id"] == str(tenant_id)
        assert stats["entry_count"] == 2
        assert stats["max_entries"] == 3  # From fixture

    def test_get_stats_global(
        self,
        cache: ConnectorCache,
        user_id: uuid.UUID,
    ) -> None:
        """Test get_stats returns global statistics."""
        tenant1 = uuid.uuid4()
        tenant2 = uuid.uuid4()

        cache.set(tenant1, "test", "op", user_id, {}, "value1")
        cache.set(tenant2, "test", "op", user_id, {}, "value2")

        stats = cache.get_stats()

        assert stats["tenant_count"] == 2
        assert stats["total_entries"] == 2
