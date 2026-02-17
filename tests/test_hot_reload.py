"""Tests for the HotReloadWatcher.

Coverage:
  - File change detection via polling snapshots
  - Plugin reload (module eviction + re-import)
  - Dev-only activation guard
  - Async background task start/stop
  - get_watched_plugins listing
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.plugins.hot_reload import HotReloadWatcher, _diff_snapshots, _evict_modules


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def dev_watcher() -> HotReloadWatcher:
    """A HotReloadWatcher in dev mode."""
    return HotReloadWatcher(dev_mode=True, poll_interval=0.05)


@pytest.fixture
def prod_watcher() -> HotReloadWatcher:
    """A HotReloadWatcher NOT in dev mode (simulates production)."""
    return HotReloadWatcher(dev_mode=False)


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    """Create a minimal plugin directory with a plugin.py."""
    pdir = tmp_path / "my-plugin"
    pdir.mkdir()
    (pdir / "__init__.py").write_text("# plugin init\n")
    (pdir / "plugin.py").write_text(
        "class MyPlugin:\n    name = 'my-plugin'\n"
    )
    return pdir


# ------------------------------------------------------------------ #
# Snapshot diffing tests
# ------------------------------------------------------------------ #


class TestSnapshotDiffing:
    """Tests for the snapshot comparison helper."""

    def test_no_changes_returns_empty(self) -> None:
        """Identical snapshots should produce no changed files."""
        snap = {"plugin.py": 1234567.0, "__init__.py": 9876543.0}
        changed = _diff_snapshots(snap, snap.copy())
        assert changed == []

    def test_modified_file_detected(self) -> None:
        """A changed mtime should be detected as a modified file."""
        old = {"plugin.py": 1000.0}
        new = {"plugin.py": 2000.0}  # mtime changed
        changed = _diff_snapshots(old, new)
        assert "plugin.py" in changed

    def test_new_file_detected(self) -> None:
        """A file added to the snapshot should be detected."""
        old = {"plugin.py": 1000.0}
        new = {"plugin.py": 1000.0, "helpers.py": 2000.0}
        changed = _diff_snapshots(old, new)
        assert "helpers.py" in changed

    def test_deleted_file_detected(self) -> None:
        """A file removed from the snapshot should be detected."""
        old = {"plugin.py": 1000.0, "helpers.py": 2000.0}
        new = {"plugin.py": 1000.0}  # helpers.py removed
        changed = _diff_snapshots(old, new)
        assert "helpers.py" in changed

    def test_multiple_changes_detected(self) -> None:
        """Multiple changes in one diff cycle should all be returned."""
        old = {"a.py": 1.0, "b.py": 2.0, "c.py": 3.0}
        new = {"a.py": 1.0, "b.py": 9.0, "d.py": 4.0}  # b changed, c deleted, d added
        changed = _diff_snapshots(old, new)
        assert "b.py" in changed
        assert "c.py" in changed
        assert "d.py" in changed


# ------------------------------------------------------------------ #
# Dev mode guard tests
# ------------------------------------------------------------------ #


class TestDevModeGuard:
    """Tests for the dev-only activation guard."""

    def test_watch_raises_in_prod_mode(
        self, prod_watcher: HotReloadWatcher, tmp_path: Path
    ) -> None:
        """watch() should raise RuntimeError in production mode."""
        with pytest.raises(RuntimeError, match="dev mode"):
            prod_watcher.watch(str(tmp_path))

    def test_reload_raises_in_prod_mode(
        self, prod_watcher: HotReloadWatcher
    ) -> None:
        """reload_plugin() should raise RuntimeError in production mode."""
        with pytest.raises(RuntimeError, match="dev mode"):
            prod_watcher.reload_plugin("my-plugin")

    def test_poll_returns_empty_in_prod_mode(
        self, prod_watcher: HotReloadWatcher
    ) -> None:
        """poll() should silently return empty list in production mode."""
        result = prod_watcher.poll()
        assert result == []

    @pytest.mark.asyncio
    async def test_start_raises_in_prod_mode(
        self, prod_watcher: HotReloadWatcher
    ) -> None:
        """start() should raise RuntimeError in production mode."""
        with pytest.raises(RuntimeError, match="dev mode"):
            await prod_watcher.start()

    def test_is_dev_mode_true(self, dev_watcher: HotReloadWatcher) -> None:
        """is_dev_mode should return True for dev watcher."""
        assert dev_watcher.is_dev_mode is True

    def test_is_dev_mode_false(self, prod_watcher: HotReloadWatcher) -> None:
        """is_dev_mode should return False for prod watcher."""
        assert prod_watcher.is_dev_mode is False


# ------------------------------------------------------------------ #
# Watch registration tests
# ------------------------------------------------------------------ #


class TestWatchRegistration:
    """Tests for watch directory registration."""

    def test_watch_registers_directory(
        self, dev_watcher: HotReloadWatcher, plugin_dir: Path
    ) -> None:
        """watch() should register the plugin directory."""
        dev_watcher.watch(str(plugin_dir))
        assert str(plugin_dir) in dev_watcher.get_watched_plugins() or \
               plugin_dir.name in dev_watcher.get_watched_plugins()

    def test_watch_idempotent(
        self, dev_watcher: HotReloadWatcher, plugin_dir: Path
    ) -> None:
        """Watching the same directory twice should not duplicate entries."""
        dev_watcher.watch(str(plugin_dir))
        dev_watcher.watch(str(plugin_dir))  # second call
        plugins = dev_watcher.get_watched_plugins()
        # Should only appear once
        assert plugins.count(plugin_dir.name) == 1

    def test_stop_watching_removes_directory(
        self, dev_watcher: HotReloadWatcher, plugin_dir: Path
    ) -> None:
        """stop_watching() should remove the directory from the watch list."""
        dev_watcher.watch(str(plugin_dir))
        assert plugin_dir.name in dev_watcher.get_watched_plugins()

        dev_watcher.stop_watching(str(plugin_dir))
        assert plugin_dir.name not in dev_watcher.get_watched_plugins()

    def test_get_watched_plugins_initially_empty(
        self, dev_watcher: HotReloadWatcher
    ) -> None:
        """get_watched_plugins() should return empty list before any watch()."""
        assert dev_watcher.get_watched_plugins() == []


# ------------------------------------------------------------------ #
# File change detection tests
# ------------------------------------------------------------------ #


class TestFileChangeDetection:
    """Tests for detecting file changes via poll()."""

    def test_poll_no_changes_returns_empty(
        self, dev_watcher: HotReloadWatcher, plugin_dir: Path
    ) -> None:
        """poll() should return empty list when no files changed."""
        dev_watcher.watch(str(plugin_dir))
        # Immediately poll - nothing should have changed
        reloaded = dev_watcher.poll()
        assert reloaded == []

    def test_poll_detects_file_modification(
        self, dev_watcher: HotReloadWatcher, plugin_dir: Path
    ) -> None:
        """poll() should detect when a watched file is modified."""
        dev_watcher.watch(str(plugin_dir))

        # Modify the file (ensure mtime changes by adding content)
        plugin_file = plugin_dir / "plugin.py"
        time.sleep(0.01)  # Ensure filesystem mtime resolution
        plugin_file.write_text("class MyPlugin:\n    name = 'modified'\n")

        # Force the timestamp to be different
        import os
        current_mtime = plugin_file.stat().st_mtime
        os.utime(plugin_file, (current_mtime + 1, current_mtime + 1))

        # Patch reload_plugin to avoid actual import
        with patch.object(dev_watcher, "reload_plugin", return_value=True) as mock_reload:
            reloaded = dev_watcher.poll()

        assert len(reloaded) > 0 or mock_reload.call_count > 0


# ------------------------------------------------------------------ #
# Plugin reload tests
# ------------------------------------------------------------------ #


class TestPluginReload:
    """Tests for reload_plugin()."""

    def test_reload_unregistered_plugin_returns_false(
        self, dev_watcher: HotReloadWatcher
    ) -> None:
        """reload_plugin for an unknown plugin should return False."""
        result = dev_watcher.reload_plugin("nonexistent-plugin")
        assert result is False

    def test_reload_registered_plugin_succeeds(
        self, dev_watcher: HotReloadWatcher, plugin_dir: Path
    ) -> None:
        """reload_plugin should return True for a valid plugin."""
        dev_watcher.watch(str(plugin_dir))

        # Patch importlib to avoid actual module loading during test
        with patch("src.plugins.hot_reload.importlib.util.spec_from_file_location") as mock_spec:
            mock_loader = MagicMock()
            mock_loader.exec_module = MagicMock()
            mock_spec_obj = MagicMock()
            mock_spec_obj.loader = mock_loader
            mock_spec.return_value = mock_spec_obj

            with patch("src.plugins.hot_reload.importlib.util.module_from_spec") as mock_module_from:
                mock_module = MagicMock()
                mock_module_from.return_value = mock_module

                result = dev_watcher.reload_plugin("my-plugin")

        assert result is True

    def test_reload_handles_import_error_gracefully(
        self, dev_watcher: HotReloadWatcher, plugin_dir: Path
    ) -> None:
        """reload_plugin should return False if import fails."""
        dev_watcher.watch(str(plugin_dir))

        with patch("src.plugins.hot_reload.importlib.util.spec_from_file_location") as mock_spec:
            mock_spec.side_effect = ImportError("module not found")
            result = dev_watcher.reload_plugin("my-plugin")

        assert result is False


# ------------------------------------------------------------------ #
# Module eviction tests
# ------------------------------------------------------------------ #


class TestModuleEviction:
    """Tests for _evict_modules helper."""

    def test_evict_removes_plugin_modules_from_sys(self, tmp_path: Path) -> None:
        """_evict_modules should remove plugin modules from sys.modules."""
        # Create a fake module with a __file__ in the plugin dir
        fake_module = MagicMock()
        fake_module.__file__ = str(tmp_path / "plugin.py")

        module_name = f"eap_test_eviction_{uuid.uuid4().hex}"
        sys.modules[module_name] = fake_module

        _evict_modules(str(tmp_path))

        assert module_name not in sys.modules

    def test_evict_leaves_unrelated_modules_intact(self, tmp_path: Path) -> None:
        """_evict_modules should not remove unrelated modules."""
        unrelated_name = f"unrelated_{uuid.uuid4().hex}"
        fake_module = MagicMock()
        fake_module.__file__ = "/some/other/path/module.py"
        sys.modules[unrelated_name] = fake_module

        try:
            _evict_modules(str(tmp_path))
            assert unrelated_name in sys.modules
        finally:
            sys.modules.pop(unrelated_name, None)


# ------------------------------------------------------------------ #
# Async background task tests
# ------------------------------------------------------------------ #


class TestAsyncBackgroundTask:
    """Tests for the async polling background task."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self, dev_watcher: HotReloadWatcher) -> None:
        """start() and stop() should work without errors."""
        await dev_watcher.start()
        assert dev_watcher._running is True

        await dev_watcher.stop()
        assert dev_watcher._running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, dev_watcher: HotReloadWatcher) -> None:
        """Calling start() twice should not create duplicate tasks."""
        await dev_watcher.start()
        task1 = dev_watcher._task

        await dev_watcher.start()  # second call
        task2 = dev_watcher._task

        assert task1 is task2  # same task, not a new one

        await dev_watcher.stop()

    @pytest.mark.asyncio
    async def test_poll_loop_runs(
        self, dev_watcher: HotReloadWatcher, plugin_dir: Path
    ) -> None:
        """Background loop should call poll() at regular intervals."""
        dev_watcher.watch(str(plugin_dir))

        poll_calls = []
        original_poll = dev_watcher.poll

        def tracking_poll() -> list[str]:
            result = original_poll()
            poll_calls.append(True)
            return result

        dev_watcher.poll = tracking_poll  # type: ignore[method-assign]

        await dev_watcher.start()
        await asyncio.sleep(0.15)  # Wait for ~3 poll cycles at 0.05s interval
        await dev_watcher.stop()

        assert len(poll_calls) >= 2, f"Expected at least 2 poll calls, got {len(poll_calls)}"
