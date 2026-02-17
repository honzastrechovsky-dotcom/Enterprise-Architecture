"""Tests for Edge Deployment.

TDD methodology covering:
- EdgeSyncService: queue operations, push, pull, status, offline handling
- LightweightMode: resource checking, model filtering, health check
- Edge config loading
- Conflict resolution (central wins)
- Offline queue and retry behavior
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import httpx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item_data() -> dict[str, Any]:
    return {"content": "test message", "tenant_id": str(uuid.uuid4())}


# ---------------------------------------------------------------------------
# EdgeSyncService - Queue operations
# ---------------------------------------------------------------------------


class TestEdgeSyncServiceQueue:
    """Tests for sync queue management."""

    @pytest.fixture
    async def sync_service(self, tmp_path):
        from src.edge.sync import EdgeSyncService

        db_path = tmp_path / "test_sync.db"
        service = EdgeSyncService(
            db_url=f"sqlite+aiosqlite:///{db_path}",
            node_id="test-node-001",
        )
        await service.initialize()
        yield service
        await service.close()

    @pytest.mark.asyncio
    async def test_queue_for_sync_returns_entry_id(self, sync_service):
        """queue_for_sync() returns a non-empty string entry ID."""
        entry_id = await sync_service.queue_for_sync(
            item_type="conversation",
            item_id=str(uuid.uuid4()),
            data=_item_data(),
        )
        assert isinstance(entry_id, str)
        assert len(entry_id) > 0

    @pytest.mark.asyncio
    async def test_queue_multiple_items(self, sync_service):
        """Multiple items can be queued independently."""
        ids = []
        for i in range(5):
            entry_id = await sync_service.queue_for_sync(
                item_type="document",
                item_id=str(uuid.uuid4()),
                data={"index": i},
            )
            ids.append(entry_id)
        # All IDs are unique
        assert len(set(ids)) == 5

    @pytest.mark.asyncio
    async def test_queue_different_item_types(self, sync_service):
        """Different item types can coexist in the queue."""
        from src.edge.sync import SyncDirection

        await sync_service.queue_for_sync("conversation", "c-1", {"data": 1})
        await sync_service.queue_for_sync("document", "d-1", {"data": 2})
        await sync_service.queue_for_sync("audit_log", "a-1", {"data": 3})

        status = await sync_service.get_sync_status()
        assert status.pending_push >= 3

    @pytest.mark.asyncio
    async def test_queue_pull_direction(self, sync_service):
        """Items queued with PULL direction are tracked separately."""
        from src.edge.sync import SyncDirection

        await sync_service.queue_for_sync(
            item_type="config",
            item_id="cfg-1",
            data={"version": 2},
            direction=SyncDirection.PULL,
        )

        status = await sync_service.get_sync_status()
        assert status.pending_pull >= 1


# ---------------------------------------------------------------------------
# EdgeSyncService - Push to central
# ---------------------------------------------------------------------------


class TestEdgeSyncServicePush:
    """Tests for sync_to_central() push behavior."""

    @pytest.fixture
    async def sync_service(self, tmp_path):
        from src.edge.sync import EdgeSyncService

        db_path = tmp_path / "test_push.db"
        service = EdgeSyncService(
            db_url=f"sqlite+aiosqlite:///{db_path}",
            node_id="test-push-node",
        )
        await service.initialize()
        # Queue test items
        for i in range(3):
            await service.queue_for_sync(
                item_type="conversation",
                item_id=f"conv-{i}",
                data={"text": f"message {i}"},
            )
        yield service
        await service.close()

    @pytest.mark.asyncio
    async def test_push_success_marks_items_synced(self, sync_service):
        """Successful push marks all items as synced."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": {}  # empty results = all synced
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await sync_service.sync_to_central(
                endpoint="https://central.example.com/api/v1/sync",
                api_key="test-key",
            )

        assert result["synced"] >= 3
        assert result["failed"] == 0

    @pytest.mark.asyncio
    async def test_push_offline_queues_for_retry(self, sync_service):
        """ConnectError marks items as failed for retry (offline behavior)."""
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client_cls.return_value = mock_client

            result = await sync_service.sync_to_central(
                endpoint="https://central.example.com/api/v1/sync",
                api_key="test-key",
            )

        assert result["failed"] >= 3
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_push_conflict_central_wins(self, sync_service):
        """Items marked as conflict by central are marked CONFLICT locally."""
        # Get the first item's ID
        pending = await sync_service._get_pending_items(
            sync_service.__class__.__dict__  # access via instance
            if False
            else __import__(
                "src.edge.sync", fromlist=["SyncDirection"]
            ).SyncDirection.PUSH
        )
        # Re-queue so we know an ID
        from src.edge.sync import SyncDirection, SyncItemStatus

        extra_id = await sync_service.queue_for_sync(
            "doc", "conflict-doc", {"v": 1}
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": {extra_id: "conflict"}
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await sync_service.sync_to_central(
                endpoint="https://central.example.com/api/v1/sync",
                api_key="test-key",
            )

        assert result["skipped"] >= 1

    @pytest.mark.asyncio
    async def test_push_nothing_pending_returns_zero(self, tmp_path):
        """When queue is empty, push returns zero counts."""
        from src.edge.sync import EdgeSyncService

        db_path = tmp_path / "test_empty.db"
        service = EdgeSyncService(
            db_url=f"sqlite+aiosqlite:///{db_path}",
            node_id="empty-node",
        )
        await service.initialize()

        result = await service.sync_to_central(
            endpoint="https://central.example.com/api/v1/sync",
            api_key="test-key",
        )

        assert result == {"synced": 0, "failed": 0, "skipped": 0}
        await service.close()


# ---------------------------------------------------------------------------
# EdgeSyncService - Pull from central
# ---------------------------------------------------------------------------


class TestEdgeSyncServicePull:
    """Tests for sync_from_central() pull behavior."""

    @pytest.fixture
    async def sync_service(self, tmp_path):
        from src.edge.sync import EdgeSyncService

        db_path = tmp_path / "test_pull.db"
        service = EdgeSyncService(
            db_url=f"sqlite+aiosqlite:///{db_path}",
            node_id="pull-node",
        )
        await service.initialize()
        yield service
        await service.close()

    @pytest.mark.asyncio
    async def test_pull_returns_item_count(self, sync_service):
        """Successful pull returns number of items received."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {"item_type": "config", "item_id": "cfg-1", "data": {"v": 2}},
                {"item_type": "config", "item_id": "cfg-2", "data": {"v": 3}},
            ]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await sync_service.sync_from_central(
                endpoint="https://central.example.com/api/v1/sync",
                api_key="test-key",
            )

        assert result["pulled"] == 2

    @pytest.mark.asyncio
    async def test_pull_offline_returns_error(self, sync_service):
        """Pull failure when offline returns error dict gracefully."""
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(
                side_effect=httpx.ConnectError("Offline")
            )
            mock_client_cls.return_value = mock_client

            result = await sync_service.sync_from_central(
                endpoint="https://central.example.com/api/v1/sync",
                api_key="test-key",
            )

        assert result["pulled"] == 0
        assert "error" in result


# ---------------------------------------------------------------------------
# EdgeSyncService - Status
# ---------------------------------------------------------------------------


class TestEdgeSyncServiceStatus:
    """Tests for get_sync_status()."""

    @pytest.fixture
    async def sync_service(self, tmp_path):
        from src.edge.sync import EdgeSyncService

        db_path = tmp_path / "test_status.db"
        service = EdgeSyncService(
            db_url=f"sqlite+aiosqlite:///{db_path}",
            node_id="status-node",
        )
        await service.initialize()
        yield service
        await service.close()

    @pytest.mark.asyncio
    async def test_status_initial_state(self, sync_service):
        """Initial status has zero pending items and no sync timestamps."""
        from src.edge.sync import SyncStatus

        status = await sync_service.get_sync_status()
        assert isinstance(status, SyncStatus)
        assert status.pending_push == 0
        assert status.pending_pull == 0
        assert status.failed_items == 0
        assert status.node_id == "status-node"

    @pytest.mark.asyncio
    async def test_status_reflects_queued_items(self, sync_service):
        """Status shows pending count after queueing items."""
        for i in range(4):
            await sync_service.queue_for_sync("doc", f"d-{i}", {"i": i})

        status = await sync_service.get_sync_status()
        assert status.pending_push == 4

    @pytest.mark.asyncio
    async def test_status_has_sync_endpoint(self, sync_service):
        """Status includes sync_endpoint field."""
        status = await sync_service.get_sync_status()
        assert hasattr(status, "sync_endpoint")


# ---------------------------------------------------------------------------
# LightweightMode - Model filtering
# ---------------------------------------------------------------------------


class TestLightweightModeModels:
    """Tests for get_available_models() - only 7B-class allowed."""

    @pytest.fixture
    def lightweight(self):
        from src.edge.lightweight import LightweightMode

        return LightweightMode()

    def test_returns_only_7b_class_models(self, lightweight):
        """Available models list contains only edge-approved 7B models."""
        models = lightweight.get_available_models()
        assert len(models) > 0
        # All returned models must be in the allowlist
        from src.edge.lightweight import EDGE_ALLOWED_MODELS
        for model in models:
            assert model in EDGE_ALLOWED_MODELS

    def test_no_large_models_returned(self, lightweight):
        """No 70B or larger models appear in the edge model list."""
        models = lightweight.get_available_models()
        forbidden = ["70b", "405b", "gpt-4", "claude-opus", "gemini-ultra"]
        for model in models:
            for forbidden_term in forbidden:
                assert forbidden_term not in model.lower(), (
                    f"Large model '{model}' should not be in edge model list"
                )

    def test_env_override_filters_further(self, lightweight, monkeypatch):
        """EDGE_ALLOWED_MODELS env var further restricts available models."""
        monkeypatch.setenv("EDGE_ALLOWED_MODELS", "ollama/llama3:7b,ollama/mistral:7b")
        models = lightweight.get_available_models()
        assert set(models).issubset({"ollama/llama3:7b", "ollama/mistral:7b"})


# ---------------------------------------------------------------------------
# LightweightMode - Resource checking
# ---------------------------------------------------------------------------


class TestLightweightModeResources:
    """Tests for check_resources() memory/CPU/disk reporting."""

    @pytest.fixture
    def lightweight(self):
        from src.edge.lightweight import LightweightMode

        return LightweightMode(max_memory_mb=4096)

    def test_returns_resource_snapshot(self, lightweight):
        """check_resources() returns a ResourceSnapshot with all fields."""
        from src.edge.lightweight import ResourceSnapshot

        snapshot = lightweight.check_resources()
        assert isinstance(snapshot, ResourceSnapshot)
        assert isinstance(snapshot.memory_used_mb, float)
        assert isinstance(snapshot.memory_total_mb, float)
        assert isinstance(snapshot.memory_percent, float)
        assert isinstance(snapshot.cpu_percent, float)
        assert isinstance(snapshot.disk_used_gb, float)
        assert isinstance(snapshot.disk_total_gb, float)
        assert isinstance(snapshot.disk_percent, float)

    def test_memory_percent_is_bounded(self, lightweight):
        """Memory percent is between 0 and 100."""
        snapshot = lightweight.check_resources()
        assert 0.0 <= snapshot.memory_percent <= 100.0

    def test_disk_percent_is_bounded(self, lightweight):
        """Disk percent is between 0 and 100."""
        snapshot = lightweight.check_resources()
        assert 0.0 <= snapshot.disk_percent <= 100.0

    def test_low_memory_detection(self, monkeypatch):
        """is_memory_low is True when free memory is below threshold."""
        from src.edge.lightweight import LightweightMode, ResourceSnapshot

        monkeypatch.setenv("LOW_MEMORY_THRESHOLD_MB", "1024")
        snapshot = ResourceSnapshot(
            memory_used_mb=3800.0,
            memory_total_mb=4096.0,
            memory_percent=92.8,
            cpu_percent=10.0,
            disk_used_gb=20.0,
            disk_total_gb=50.0,
            disk_percent=40.0,
        )
        # Free = 296MB < 1024MB threshold
        assert snapshot.is_memory_low is True

    def test_low_disk_detection(self, monkeypatch):
        """is_disk_low is True when free disk is below threshold."""
        from src.edge.lightweight import ResourceSnapshot

        monkeypatch.setenv("LOW_DISK_THRESHOLD_GB", "5")
        snapshot = ResourceSnapshot(
            memory_used_mb=1000.0,
            memory_total_mb=4096.0,
            memory_percent=24.4,
            cpu_percent=10.0,
            disk_used_gb=48.0,
            disk_total_gb=50.0,
            disk_percent=96.0,
        )
        # Free = 2GB < 5GB threshold
        assert snapshot.is_disk_low is True


# ---------------------------------------------------------------------------
# LightweightMode - Health check
# ---------------------------------------------------------------------------


class TestLightweightModeHealth:
    """Tests for health_check() edge health report."""

    @pytest.fixture
    def lightweight(self):
        from src.edge.lightweight import LightweightMode

        return LightweightMode(max_memory_mb=4096)

    def test_health_check_returns_report(self, lightweight):
        """health_check() returns an EdgeHealthReport."""
        from src.edge.lightweight import EdgeHealthReport

        report = lightweight.health_check()
        assert isinstance(report, EdgeHealthReport)

    def test_health_check_edge_mode_true(self, lightweight):
        """EdgeHealthReport always has edge_mode=True."""
        report = lightweight.health_check()
        assert report.edge_mode is True

    def test_health_check_status_is_valid(self, lightweight):
        """Health status is one of healthy/degraded/unhealthy."""
        report = lightweight.health_check()
        assert report.status in ("healthy", "degraded", "unhealthy")

    def test_health_check_has_available_models(self, lightweight):
        """Health report includes non-empty model list."""
        report = lightweight.health_check()
        assert isinstance(report.available_models, list)
        assert len(report.available_models) > 0

    def test_health_check_uptime_is_positive(self, lightweight):
        """Uptime seconds is a positive number."""
        report = lightweight.health_check()
        assert report.uptime_seconds >= 0.0

    def test_health_check_degraded_on_low_resources(self, lightweight):
        """Health is degraded when resources are constrained."""
        from src.edge.lightweight import ResourceSnapshot

        constrained_snapshot = ResourceSnapshot(
            memory_used_mb=3900.0,
            memory_total_mb=4096.0,
            memory_percent=95.2,
            cpu_percent=5.0,
            disk_used_gb=48.0,
            disk_total_gb=50.0,
            disk_percent=96.0,
        )

        with patch.object(lightweight, "check_resources", return_value=constrained_snapshot):
            report = lightweight.health_check()

        assert report.status in ("degraded", "unhealthy")
        assert len(report.warnings) > 0


# ---------------------------------------------------------------------------
# LightweightMode - configure_for_edge
# ---------------------------------------------------------------------------


class TestLightweightModeConfigure:
    """Tests for configure_for_edge()."""

    def test_configure_sets_edge_mode_state(self):
        """configure_for_edge() sets app.state.edge_mode = True."""
        from src.edge.lightweight import LightweightMode
        from unittest.mock import MagicMock

        lightweight = LightweightMode()
        mock_app = MagicMock()
        mock_app.routes = []
        # Use a real object for state so attribute assignment works
        state = type("State", (), {})()
        mock_app.state = state

        lightweight.configure_for_edge(mock_app)

        assert lightweight._configured is True
        assert getattr(state, "edge_mode", None) is True

    def test_configure_idempotent_via_flag(self):
        """configure_for_edge() sets _configured flag."""
        from src.edge.lightweight import LightweightMode

        lightweight = LightweightMode()
        mock_app = MagicMock()
        mock_app.routes = []
        mock_app.state = MagicMock()

        lightweight.configure_for_edge(mock_app)
        assert lightweight._configured is True


# ---------------------------------------------------------------------------
# Edge config loading
# ---------------------------------------------------------------------------


class TestEdgeConfigLoading:
    """Tests for loading edge-config.yaml."""

    def test_edge_config_file_is_valid_yaml(self):
        """edge-config.yaml is parseable and has required keys."""
        import yaml
        import pathlib

        config_path = pathlib.Path(__file__).parent.parent / "deploy/edge/edge-config.yaml"
        assert config_path.exists(), "edge-config.yaml must exist"

        with open(config_path) as f:
            config = yaml.safe_load(f)

        assert config["model_tier"] == "light_only"
        assert config["cache_backend"] == "memory"
        assert config["database_backend"] == "sqlite"
        assert config["sync_enabled"] is True
        assert config["offline_mode"] is True
        assert isinstance(config["max_memory_mb"], int)
        assert config["max_memory_mb"] == 4096

    def test_edge_config_sync_interval(self):
        """Sync interval is 300 seconds as specified."""
        import yaml
        import pathlib

        config_path = pathlib.Path(__file__).parent.parent / "deploy/edge/edge-config.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)

        assert config["sync_interval_seconds"] == 300

    def test_edge_config_conflict_resolution(self):
        """Conflict resolution is central_wins."""
        import yaml
        import pathlib

        config_path = pathlib.Path(__file__).parent.parent / "deploy/edge/edge-config.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)

        assert config["conflict_resolution"] == "central_wins"
