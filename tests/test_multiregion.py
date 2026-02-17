"""Tests for Multi-Region Support.

TDD methodology covering:
- RegionRouter: optimal region selection, data residency enforcement, health probing
- ReplicationManager: topology configuration, lag monitoring, promotion, tenant migration
- FailoverManager: failure detection, failover execution, status, rollback
- Regions API: list, health, failover, residency endpoints
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenant() -> str:
    return str(uuid.uuid4())


def _region_cfg(
    name: str,
    endpoint: str = "https://region.example.com",
    is_primary: bool = False,
    allowed_tenants: list[str] | None = None,
    excluded_tenants: list[str] | None = None,
) -> "RegionConfig":
    from src.multiregion.routing import RegionConfig, RegionStatus

    return RegionConfig(
        name=name,
        endpoint=endpoint,
        is_primary=is_primary,
        allowed_tenants=allowed_tenants or [],
        excluded_tenants=excluded_tenants or [],
        status=RegionStatus.HEALTHY,
    )


# ---------------------------------------------------------------------------
# RegionRouter - Region management
# ---------------------------------------------------------------------------


class TestRegionRouterSetup:
    """Tests for region registration and configuration."""

    def test_register_region_makes_it_available(self):
        """register_region() makes region available via get_region()."""
        from src.multiregion.routing import RegionRouter

        router = RegionRouter()
        region = _region_cfg("us-east-1", is_primary=True)
        router.register_region(region)

        result = router.get_region("us-east-1")
        assert result is not None
        assert result.name == "us-east-1"

    def test_list_regions_returns_all_registered(self):
        """list_regions() returns all registered regions."""
        from src.multiregion.routing import RegionRouter

        router = RegionRouter()
        for name in ["us-east-1", "eu-west-1", "ap-southeast-1"]:
            router.register_region(_region_cfg(name))

        assert len(router.list_regions()) == 3

    def test_get_unknown_region_returns_none(self):
        """get_region() returns None for an unknown region name."""
        from src.multiregion.routing import RegionRouter

        router = RegionRouter()
        assert router.get_region("nonexistent") is None

    def test_constructor_accepts_regions_list(self):
        """RegionRouter can be initialised with a list of regions."""
        from src.multiregion.routing import RegionRouter

        regions = [
            _region_cfg("us-east-1", is_primary=True),
            _region_cfg("eu-west-1"),
        ]
        router = RegionRouter(regions=regions)
        assert len(router.list_regions()) == 2


# ---------------------------------------------------------------------------
# RegionRouter - Optimal region selection
# ---------------------------------------------------------------------------


class TestRegionRouterOptimal:
    """Tests for get_optimal_region() routing logic."""

    @pytest.fixture
    def router_with_regions(self):
        from src.multiregion.routing import RegionRouter

        router = RegionRouter()
        router.register_region(_region_cfg("us-east-1", is_primary=True))
        router.register_region(_region_cfg("eu-west-1"))
        router.register_region(_region_cfg("ap-southeast-1"))
        return router

    def test_returns_primary_when_no_pin(self, router_with_regions):
        """Without a residency pin, selects the primary region."""
        tenant_id = _make_tenant()
        region = router_with_regions.get_optimal_region(tenant_id)
        assert region is not None
        assert region.is_primary is True
        assert region.name == "us-east-1"

    def test_returns_pinned_region(self, router_with_regions):
        """Returns the pinned region when tenant has residency."""
        tenant_id = _make_tenant()
        router_with_regions.set_tenant_residency(tenant_id, "eu-west-1")

        region = router_with_regions.get_optimal_region(tenant_id)
        assert region is not None
        assert region.name == "eu-west-1"

    def test_returns_none_when_no_healthy_region(self):
        """Returns None when all regions are unavailable."""
        from src.multiregion.routing import RegionRouter, RegionStatus

        router = RegionRouter()
        region = _region_cfg("us-east-1", is_primary=True)
        region.status = RegionStatus.UNAVAILABLE
        router.register_region(region)

        result = router.get_optimal_region(_make_tenant())
        assert result is None

    def test_skips_unavailable_primary_falls_back_to_secondary(self):
        """Falls back to healthy secondary when primary is unavailable."""
        from src.multiregion.routing import RegionRouter, RegionStatus

        router = RegionRouter()
        primary = _region_cfg("us-east-1", is_primary=True)
        primary.status = RegionStatus.UNAVAILABLE
        secondary = _region_cfg("eu-west-1", is_primary=False)
        router.register_region(primary)
        router.register_region(secondary)

        result = router.get_optimal_region(_make_tenant())
        assert result is not None
        assert result.name == "eu-west-1"

    def test_lowest_latency_secondary_preferred(self):
        """When multiple secondaries available, lowest latency wins."""
        from src.multiregion.routing import RegionRouter, RegionStatus

        router = RegionRouter()
        primary = _region_cfg("us-east-1", is_primary=True)
        primary.status = RegionStatus.UNAVAILABLE

        eu = _region_cfg("eu-west-1")
        eu.latency_ms = 120.0

        ap = _region_cfg("ap-southeast-1")
        ap.latency_ms = 45.0  # lowest

        router.register_region(primary)
        router.register_region(eu)
        router.register_region(ap)

        result = router.get_optimal_region(_make_tenant())
        assert result is not None
        assert result.name == "ap-southeast-1"


# ---------------------------------------------------------------------------
# RegionRouter - Data residency
# ---------------------------------------------------------------------------


class TestRegionRouterResidency:
    """Tests for check_data_residency() enforcement."""

    @pytest.fixture
    def router(self):
        from src.multiregion.routing import RegionRouter

        router = RegionRouter()
        # EU region: only specific tenants
        tenant_1 = _make_tenant()
        eu_region = _region_cfg("eu-west-1", allowed_tenants=[tenant_1])
        router.register_region(eu_region)
        router._test_tenant_1 = tenant_1
        return router

    def test_allowed_tenant_passes_residency_check(self, router):
        """check_data_residency() returns True for allowed tenant."""
        tenant_1 = router._test_tenant_1
        assert router.check_data_residency(tenant_1, "eu-west-1") is True

    def test_disallowed_tenant_fails_residency_check(self, router):
        """check_data_residency() returns False for tenant not in allowed list."""
        other_tenant = _make_tenant()
        assert router.check_data_residency(other_tenant, "eu-west-1") is False

    def test_excluded_tenant_fails_residency_check(self):
        """Tenant in excluded_tenants is denied even if allowed_tenants is empty."""
        from src.multiregion.routing import RegionRouter

        excluded_tenant = _make_tenant()
        router = RegionRouter()
        region = _region_cfg("us-east-1", excluded_tenants=[excluded_tenant])
        router.register_region(region)

        assert router.check_data_residency(excluded_tenant, "us-east-1") is False

    def test_unknown_region_fails_residency_check(self):
        """check_data_residency() returns False for an unknown region."""
        from src.multiregion.routing import RegionRouter

        router = RegionRouter()
        assert router.check_data_residency(_make_tenant(), "nonexistent-region") is False

    def test_set_tenant_residency_raises_for_unknown_region(self):
        """set_tenant_residency() raises ValueError for unknown region."""
        from src.multiregion.routing import RegionRouter

        router = RegionRouter()
        with pytest.raises(ValueError, match="Unknown region"):
            router.set_tenant_residency(_make_tenant(), "does-not-exist")

    def test_route_request_respects_residency(self):
        """route_request() routes to pinned region."""
        from src.multiregion.routing import RegionRouter

        tenant_id = _make_tenant()
        router = RegionRouter()
        router.register_region(_region_cfg("us-east-1", is_primary=True))
        router.register_region(_region_cfg("eu-west-1"))
        router.set_tenant_residency(tenant_id, "eu-west-1")

        decision = router.route_request(tenant_id)
        assert decision is not None
        assert decision.region_name == "eu-west-1"
        assert "residency" in decision.reason


# ---------------------------------------------------------------------------
# RegionRouter - Health probing
# ---------------------------------------------------------------------------


class TestRegionRouterHealth:
    """Tests for get_region_health()."""

    @pytest.mark.asyncio
    async def test_health_returns_all_regions(self):
        """get_region_health() returns entry for every registered region."""
        from src.multiregion.routing import RegionRouter

        router = RegionRouter()
        router.register_region(_region_cfg("us-east-1", endpoint="http://us.example.com"))
        router.register_region(_region_cfg("eu-west-1", endpoint="http://eu.example.com"))

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            health = await router.get_region_health()

        assert "us-east-1" in health
        assert "eu-west-1" in health

    @pytest.mark.asyncio
    async def test_health_marks_unavailable_on_connect_error(self):
        """get_region_health() marks region unavailable when probe fails."""
        from src.multiregion.routing import RegionRouter, RegionStatus

        router = RegionRouter()
        router.register_region(_region_cfg("us-east-1", endpoint="http://dead.example.com"))

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_cls.return_value = mock_client

            health = await router.get_region_health()

        assert health["us-east-1"]["status"] == RegionStatus.UNAVAILABLE.value
        assert health["us-east-1"]["healthy"] is False


# ---------------------------------------------------------------------------
# ReplicationManager - Configuration
# ---------------------------------------------------------------------------


class TestReplicationManagerConfig:
    """Tests for configure_replication()."""

    def test_configure_sets_primary(self):
        """configure_replication() records the primary region."""
        from src.multiregion.replication import ReplicationManager, ReplicationRole

        mgr = ReplicationManager()
        mgr.configure_replication("us-east-1", ["eu-west-1", "ap-southeast-1"])

        topology = mgr.get_topology()
        assert topology["primary_region"] == "us-east-1"
        assert topology["states"]["us-east-1"]["role"] == ReplicationRole.PRIMARY.value

    def test_configure_sets_replicas(self):
        """configure_replication() records replica regions."""
        from src.multiregion.replication import ReplicationManager, ReplicationRole

        mgr = ReplicationManager()
        mgr.configure_replication("us-east-1", ["eu-west-1", "ap-southeast-1"])

        topology = mgr.get_topology()
        assert "eu-west-1" in topology["states"]
        assert topology["states"]["eu-west-1"]["role"] == ReplicationRole.REPLICA.value

    def test_primary_has_zero_lag(self):
        """Primary region reports 0.0 replication lag."""
        from src.multiregion.replication import ReplicationManager

        mgr = ReplicationManager()
        mgr.configure_replication("us-east-1", [])

        topology = mgr.get_topology()
        assert topology["states"]["us-east-1"]["lag_seconds"] == 0.0


# ---------------------------------------------------------------------------
# ReplicationManager - Lag monitoring
# ---------------------------------------------------------------------------


class TestReplicationManagerLag:
    """Tests for get_replication_lag()."""

    @pytest.fixture
    def mgr_with_replicas(self):
        from src.multiregion.replication import ReplicationManager

        mgr = ReplicationManager()
        mgr.configure_replication(
            primary_region="us-east-1",
            replica_regions=["eu-west-1"],
            region_endpoints={
                "eu-west-1": "http://eu.example.com",
            },
            api_key="test-key",
        )
        return mgr

    @pytest.mark.asyncio
    async def test_primary_lag_is_zero(self, mgr_with_replicas):
        """get_replication_lag() returns 0.0 for the primary region."""
        lag = await mgr_with_replicas.get_replication_lag("us-east-1")
        assert lag == 0.0

    @pytest.mark.asyncio
    async def test_replica_lag_from_api(self, mgr_with_replicas):
        """get_replication_lag() returns value from management API."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"lag_seconds": 2.5}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            lag = await mgr_with_replicas.get_replication_lag("eu-west-1")

        assert lag == 2.5

    @pytest.mark.asyncio
    async def test_lag_returns_negative_one_on_error(self, mgr_with_replicas):
        """get_replication_lag() returns -1.0 when region is unreachable."""
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
            mock_cls.return_value = mock_client

            lag = await mgr_with_replicas.get_replication_lag("eu-west-1")

        assert lag == -1.0

    @pytest.mark.asyncio
    async def test_unknown_region_returns_negative_one(self, mgr_with_replicas):
        """get_replication_lag() returns -1.0 for unknown region."""
        lag = await mgr_with_replicas.get_replication_lag("nonexistent")
        assert lag == -1.0


# ---------------------------------------------------------------------------
# ReplicationManager - Promotion
# ---------------------------------------------------------------------------


class TestReplicationManagerPromotion:
    """Tests for promote_replica()."""

    @pytest.fixture
    def mgr(self):
        from src.multiregion.replication import ReplicationManager

        mgr = ReplicationManager()
        mgr.configure_replication(
            primary_region="us-east-1",
            replica_regions=["eu-west-1"],
            region_endpoints={"eu-west-1": "http://eu.example.com"},
            api_key="test-key",
        )
        return mgr

    @pytest.mark.asyncio
    async def test_promote_updates_primary(self, mgr):
        """promote_replica() makes the replica the new primary."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            result = await mgr.promote_replica("eu-west-1")

        assert result["new_primary"] == "eu-west-1"
        assert result["previous_primary"] == "us-east-1"

    @pytest.mark.asyncio
    async def test_promote_topology_updated(self, mgr):
        """promote_replica() updates topology roles."""
        from src.multiregion.replication import ReplicationRole

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            await mgr.promote_replica("eu-west-1")

        topology = mgr.get_topology()
        assert topology["primary_region"] == "eu-west-1"
        assert topology["states"]["eu-west-1"]["role"] == ReplicationRole.PRIMARY.value
        assert topology["states"]["us-east-1"]["role"] == ReplicationRole.REPLICA.value

    @pytest.mark.asyncio
    async def test_promote_non_replica_raises(self, mgr):
        """promote_replica() raises ValueError for non-replica region."""
        with pytest.raises(ValueError, match="not a known replica"):
            await mgr.promote_replica("us-east-1")


# ---------------------------------------------------------------------------
# FailoverManager - Detection
# ---------------------------------------------------------------------------


class TestFailoverManagerDetection:
    """Tests for detect_failure()."""

    @pytest.fixture
    def failover_mgr(self):
        from src.multiregion.routing import RegionRouter
        from src.multiregion.replication import ReplicationManager
        from src.multiregion.failover import FailoverManager

        router = RegionRouter()
        router.register_region(_region_cfg("us-east-1", endpoint="http://us.example.com", is_primary=True))
        router.register_region(_region_cfg("eu-west-1", endpoint="http://eu.example.com"))

        replication = ReplicationManager()
        replication.configure_replication("us-east-1", ["eu-west-1"])

        return FailoverManager(
            router=router,
            replication=replication,
            failure_threshold=2,
            failure_check_interval_seconds=0.01,
        )

    @pytest.mark.asyncio
    async def test_detect_failure_returns_false_for_healthy(self, failover_mgr):
        """detect_failure() returns False when health probe succeeds."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            result = await failover_mgr.detect_failure("us-east-1")

        assert result is False

    @pytest.mark.asyncio
    async def test_detect_failure_returns_true_for_down_region(self, failover_mgr):
        """detect_failure() returns True after threshold consecutive failures."""
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
            mock_cls.return_value = mock_client

            result = await failover_mgr.detect_failure("us-east-1")

        assert result is True

    @pytest.mark.asyncio
    async def test_detect_failure_unknown_region_returns_false(self, failover_mgr):
        """detect_failure() returns False for an unknown region."""
        result = await failover_mgr.detect_failure("nonexistent-region")
        assert result is False


# ---------------------------------------------------------------------------
# FailoverManager - Failover execution
# ---------------------------------------------------------------------------


class TestFailoverManagerExecution:
    """Tests for trigger_failover()."""

    @pytest.fixture
    def failover_mgr(self):
        from src.multiregion.routing import RegionRouter
        from src.multiregion.replication import ReplicationManager
        from src.multiregion.failover import FailoverManager

        router = RegionRouter()
        router.register_region(_region_cfg("us-east-1", endpoint="http://us.example.com", is_primary=True))
        router.register_region(_region_cfg("eu-west-1", endpoint="http://eu.example.com"))

        replication = ReplicationManager()
        replication.configure_replication(
            "us-east-1",
            ["eu-west-1"],
            region_endpoints={"eu-west-1": "http://eu.example.com"},
            api_key="test-key",
        )

        return FailoverManager(
            router=router,
            replication=replication,
            failure_threshold=1,
        )

    @pytest.mark.asyncio
    async def test_trigger_failover_returns_record(self, failover_mgr):
        """trigger_failover() returns a completed FailoverRecord."""
        from src.multiregion.failover import FailoverState

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            record = await failover_mgr.trigger_failover(
                failed_region="us-east-1",
                target_region="eu-west-1",
                initiated_by="manual:admin",
            )

        assert record.state == FailoverState.COMPLETE
        assert record.failed_region == "us-east-1"
        assert record.target_region == "eu-west-1"
        assert record.completed_at is not None

    @pytest.mark.asyncio
    async def test_failover_raises_when_already_active(self, failover_mgr):
        """trigger_failover() raises RuntimeError when failover in progress."""
        # Simulate active failover
        failover_mgr._active_failover = "us-east-1"

        with pytest.raises(RuntimeError, match="already in progress"):
            await failover_mgr.trigger_failover("us-east-1", "eu-west-1")

    @pytest.mark.asyncio
    async def test_failover_marks_failed_region_unavailable(self, failover_mgr):
        """trigger_failover() marks the failed region unavailable in the router."""
        from src.multiregion.routing import RegionStatus

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            await failover_mgr.trigger_failover("us-east-1", "eu-west-1")

        failed = failover_mgr._router.get_region("us-east-1")
        assert failed.status == RegionStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# FailoverManager - Status and rollback
# ---------------------------------------------------------------------------


class TestFailoverManagerStatus:
    """Tests for get_failover_status() and rollback_failover()."""

    @pytest.fixture
    def failover_mgr(self):
        from src.multiregion.routing import RegionRouter
        from src.multiregion.replication import ReplicationManager
        from src.multiregion.failover import FailoverManager

        router = RegionRouter()
        router.register_region(_region_cfg("us-east-1", endpoint="http://us.example.com", is_primary=True))
        router.register_region(_region_cfg("eu-west-1", endpoint="http://eu.example.com"))

        replication = ReplicationManager()
        replication.configure_replication(
            "us-east-1",
            ["eu-west-1"],
            region_endpoints={"eu-west-1": "http://eu.example.com"},
            api_key="test-key",
        )

        return FailoverManager(router=router, replication=replication)

    def test_initial_status_no_active_failover(self, failover_mgr):
        """Initial failover status shows no active failover."""
        status = failover_mgr.get_failover_status()
        assert status["is_failover_active"] is False
        assert status["active_failover"] is None
        assert status["total_failovers"] == 0

    @pytest.mark.asyncio
    async def test_status_shows_history_after_failover(self, failover_mgr):
        """get_failover_status() shows history after a completed failover."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            await failover_mgr.trigger_failover("us-east-1", "eu-west-1")

        status = failover_mgr.get_failover_status()
        assert status["total_failovers"] == 1
        assert len(status["history"]) == 1

    @pytest.mark.asyncio
    async def test_rollback_raises_when_active_failover(self, failover_mgr):
        """rollback_failover() raises RuntimeError during active failover."""
        failover_mgr._active_failover = "us-east-1"

        with pytest.raises(RuntimeError, match="Cannot rollback"):
            await failover_mgr.rollback_failover("us-east-1")

    @pytest.mark.asyncio
    async def test_rollback_raises_for_unavailable_region(self, failover_mgr):
        """rollback_failover() raises ValueError for unavailable region."""
        from src.multiregion.routing import RegionStatus

        region = failover_mgr._router.get_region("us-east-1")
        region.status = RegionStatus.UNAVAILABLE

        with pytest.raises(ValueError, match="still unavailable"):
            await failover_mgr.rollback_failover("us-east-1")

    @pytest.mark.asyncio
    async def test_rollback_restores_primary(self, failover_mgr):
        """rollback_failover() restores original region as primary."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            # Failover to eu-west-1
            await failover_mgr.trigger_failover("us-east-1", "eu-west-1")

        # Restore us-east-1 to healthy
        from src.multiregion.routing import RegionStatus

        us_region = failover_mgr._router.get_region("us-east-1")
        us_region.status = RegionStatus.HEALTHY

        # Patch promote_replica for rollback
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            result = await failover_mgr.rollback_failover("us-east-1")

        assert result["rolled_back_to"] == "us-east-1"
        restored = failover_mgr._router.get_region("us-east-1")
        assert restored.is_primary is True


# ---------------------------------------------------------------------------
# Regions API endpoints
# ---------------------------------------------------------------------------


class TestRegionsAPI:
    """Tests for /api/v1/regions FastAPI endpoints."""

    @pytest.fixture
    def mock_router_and_failover(self):
        from src.multiregion.routing import RegionRouter, RegionStatus
        from src.multiregion.replication import ReplicationManager
        from src.multiregion.failover import FailoverManager

        region_router = RegionRouter()
        region_router.register_region(
            _region_cfg("us-east-1", endpoint="http://us.example.com", is_primary=True)
        )
        region_router.register_region(
            _region_cfg("eu-west-1", endpoint="http://eu.example.com")
        )

        replication = ReplicationManager()
        replication.configure_replication("us-east-1", ["eu-west-1"])

        failover = FailoverManager(router=region_router, replication=replication)

        return region_router, failover

    @pytest.fixture
    def api_client(self, mock_router_and_failover):
        """FastAPI test client with regions router and mocked auth."""
        import httpx as _httpx
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.api import regions as regions_module
        from src.api.regions import router, configure_region_services

        region_router, failover = mock_router_and_failover
        configure_region_services(region_router, failover)

        app = FastAPI()

        # Override auth dependency
        from src.auth.dependencies import get_current_user
        from src.models.user import UserRole

        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        mock_user = MagicMock()
        mock_user.id = user_id
        mock_user.tenant_id = tenant_id
        mock_user.external_id = "admin-user"
        mock_user.role = "admin"

        app.dependency_overrides[get_current_user] = lambda: mock_user

        # Mock check_permission to always pass
        with patch("src.api.regions.check_permission"):
            app.include_router(router, prefix="/api/v1")
            client = TestClient(app, raise_server_exceptions=True)
            yield client, mock_user, region_router, failover

    def test_list_regions_returns_200(self, api_client):
        """GET /api/v1/regions returns 200 with region list."""
        client, user, region_router, failover = api_client

        with patch.object(region_router, "get_region_health", new_callable=AsyncMock) as mock_health:
            mock_health.return_value = {
                "us-east-1": {"status": "healthy", "latency_ms": 10.0},
                "eu-west-1": {"status": "healthy", "latency_ms": 80.0},
            }
            with patch("src.api.regions.check_permission"):
                response = client.get("/api/v1/regions")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_get_region_health_returns_detail(self, api_client):
        """GET /api/v1/regions/{name}/health returns region detail."""
        client, user, region_router, failover = api_client

        with patch.object(region_router, "get_region_health", new_callable=AsyncMock) as mock_health:
            mock_health.return_value = {
                "us-east-1": {"status": "healthy", "latency_ms": 10.0},
            }
            with patch("src.api.regions.check_permission"):
                response = client.get("/api/v1/regions/us-east-1/health")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "us-east-1"

    def test_get_unknown_region_returns_404(self, api_client):
        """GET /api/v1/regions/{name}/health returns 404 for unknown region."""
        client, _, _, _ = api_client

        with patch("src.api.regions.check_permission"):
            response = client.get("/api/v1/regions/nonexistent/health")

        assert response.status_code == 404

    def test_get_tenant_residency_returns_info(self, api_client):
        """GET /api/v1/regions/tenant/{id}/residency returns residency info."""
        client, user, region_router, _ = api_client

        # Use the user's own tenant (non-admin path)
        tenant_id = str(user.tenant_id)

        with patch("src.api.regions.check_permission"):
            response = client.get(f"/api/v1/regions/tenant/{tenant_id}/residency")

        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == tenant_id

    def test_get_residency_invalid_uuid_returns_400(self, api_client):
        """GET /api/v1/regions/tenant/{id}/residency returns 400 for bad UUID."""
        client, _, _, _ = api_client

        with patch("src.api.regions.check_permission"):
            response = client.get("/api/v1/regions/tenant/not-a-uuid/residency")

        assert response.status_code == 400

    def test_put_tenant_residency_pins_to_region(self, api_client):
        """PUT /api/v1/regions/tenant/{id}/residency pins tenant to region."""
        client, user, region_router, _ = api_client
        tenant_id = str(uuid.uuid4())

        with patch("src.api.regions.check_permission"):
            response = client.put(
                f"/api/v1/regions/tenant/{tenant_id}/residency",
                json={"region_name": "eu-west-1"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["pinned_region"] == "eu-west-1"
        assert data["data_residency_enforced"] is True

    def test_put_tenant_residency_unknown_region_returns_404(self, api_client):
        """PUT /api/v1/regions/tenant/{id}/residency returns 404 for unknown region."""
        client, _, _, _ = api_client
        tenant_id = str(uuid.uuid4())

        with patch("src.api.regions.check_permission"):
            response = client.put(
                f"/api/v1/regions/tenant/{tenant_id}/residency",
                json={"region_name": "nonexistent-region"},
            )

        assert response.status_code == 404
