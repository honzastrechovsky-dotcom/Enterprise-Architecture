"""Hot-reload watcher for plugin development mode.

``HotReloadWatcher`` polls a plugin directory for file changes and
automatically reloads modified plugins.  It uses stdlib ``os.stat``
polling to avoid the ``watchdog`` dependency.

IMPORTANT: Hot-reload is available ONLY when the EAP_DEV_MODE environment
variable is set to "true" (or the platform is configured with environment=dev).
Attempting to use hot-reload in production raises ``RuntimeError``.

Usage::

    import asyncio
    from src.plugins.hot_reload import HotReloadWatcher

    watcher = HotReloadWatcher(dev_mode=True)
    watcher.watch("/path/to/plugins")

    # In your main loop or background task:
    async def background_loop():
        while True:
            await asyncio.sleep(2)
            watcher.poll()
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class HotReloadWatcher:
    """File-change watcher for EAP plugins using polling.

    Watches one or more plugin directories and triggers reloads when
    Python source files are modified.

    Args:
        dev_mode: Must be True to activate.  Set via EAP_DEV_MODE env var
                  or pass explicitly in tests.
        poll_interval: Seconds between directory scans (default: 2.0).
    """

    def __init__(
        self,
        dev_mode: bool = False,
        poll_interval: float = 2.0,
    ) -> None:
        self._dev_mode = dev_mode or os.environ.get("EAP_DEV_MODE", "").lower() == "true"
        self._poll_interval = poll_interval

        # Map plugin_dir -> {relative_path -> last_mtime}
        self._watched_dirs: dict[str, dict[str, float]] = {}

        # Map plugin_name -> loaded module
        self._loaded_plugins: dict[str, Any] = {}

        # Map plugin_name -> plugin_dir
        self._plugin_dirs: dict[str, str] = {}

        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------- #

    def watch(self, plugin_dir: str | Path) -> None:
        """Register a directory to watch for plugin file changes.

        Scans the directory immediately to capture the baseline file state.
        Changes detected on subsequent polls trigger plugin reloads.

        Args:
            plugin_dir: Path to the plugin directory to watch.

        Raises:
            RuntimeError: If called when not in dev mode.
        """
        self._require_dev_mode("watch")

        plugin_dir = str(Path(plugin_dir).resolve())

        if plugin_dir in self._watched_dirs:
            log.debug("hot_reload.already_watching", plugin_dir=plugin_dir)
            return

        # Snapshot current state
        self._watched_dirs[plugin_dir] = self._snapshot(plugin_dir)

        # Determine plugin name from directory name
        plugin_name = Path(plugin_dir).name
        self._plugin_dirs[plugin_name] = plugin_dir

        log.info(
            "hot_reload.watching",
            plugin_dir=plugin_dir,
            plugin_name=plugin_name,
            file_count=len(self._watched_dirs[plugin_dir]),
        )

    def stop_watching(self, plugin_dir: str | Path) -> None:
        """Deregister a plugin directory from watching.

        Args:
            plugin_dir: Directory to stop watching.
        """
        plugin_dir_str = str(Path(plugin_dir).resolve())
        if plugin_dir_str in self._watched_dirs:
            del self._watched_dirs[plugin_dir_str]
            # Also remove from the plugin name -> dir mapping
            plugin_name = Path(plugin_dir_str).name
            self._plugin_dirs.pop(plugin_name, None)
            log.info("hot_reload.stopped_watching", plugin_dir=plugin_dir_str)

    def get_watched_plugins(self) -> list[str]:
        """Return names of all currently watched plugins.

        Returns:
            List of plugin name strings.
        """
        return list(self._plugin_dirs.keys())

    def poll(self) -> list[str]:
        """Perform one polling cycle and reload any changed plugins.

        Call this periodically (e.g., from an asyncio background task).
        This is the synchronous version for easy integration with event loops.

        Returns:
            List of plugin names that were reloaded.
        """
        if not self._dev_mode:
            return []

        reloaded: list[str] = []

        for plugin_dir, last_snapshot in list(self._watched_dirs.items()):
            current_snapshot = self._snapshot(plugin_dir)
            changed = _diff_snapshots(last_snapshot, current_snapshot)

            if changed:
                plugin_name = Path(plugin_dir).name
                log.info(
                    "hot_reload.change_detected",
                    plugin_name=plugin_name,
                    changed_files=changed,
                )
                self._watched_dirs[plugin_dir] = current_snapshot
                self.reload_plugin(plugin_name)
                reloaded.append(plugin_name)

        return reloaded

    def reload_plugin(self, plugin_name: str) -> bool:
        """Unload and reload a plugin by name.

        Removes the plugin's module(s) from ``sys.modules`` and re-imports
        them so changes take effect without a full process restart.

        Args:
            plugin_name: Name of the plugin to reload.

        Returns:
            True if the reload succeeded, False if the plugin is not found
            or the reload raises an exception.
        """
        self._require_dev_mode("reload_plugin")

        plugin_dir = self._plugin_dirs.get(plugin_name)
        if plugin_dir is None:
            log.warning("hot_reload.plugin_not_found", plugin_name=plugin_name)
            return False

        log.info("hot_reload.reloading", plugin_name=plugin_name)

        # Remove stale module entries to force fresh import
        _evict_modules(plugin_dir)

        # Re-import the plugin module
        plugin_py = Path(plugin_dir) / "plugin.py"
        if not plugin_py.is_file():
            log.warning(
                "hot_reload.plugin_file_missing",
                plugin_name=plugin_name,
                path=str(plugin_py),
            )
            return False

        try:
            spec = importlib.util.spec_from_file_location(
                f"eap_plugin_{plugin_name}", str(plugin_py)
            )
            if spec is None or spec.loader is None:
                log.error(
                    "hot_reload.spec_failed",
                    plugin_name=plugin_name,
                    path=str(plugin_py),
                )
                return False

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            self._loaded_plugins[plugin_name] = module
            sys.modules[f"eap_plugin_{plugin_name}"] = module

            log.info(
                "hot_reload.reloaded",
                plugin_name=plugin_name,
            )
            return True

        except Exception as exc:  # noqa: BLE001
            log.error(
                "hot_reload.reload_failed",
                plugin_name=plugin_name,
                error=str(exc),
                exc_info=True,
            )
            return False

    # ---------------------------------------------------------------- #
    # Async background task
    # ---------------------------------------------------------------- #

    async def start(self) -> None:
        """Start the background polling task.

        Runs ``poll()`` every ``poll_interval`` seconds until ``stop()``
        is called.

        Raises:
            RuntimeError: If not in dev mode.
        """
        self._require_dev_mode("start")

        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info(
            "hot_reload.started",
            poll_interval=self._poll_interval,
        )

    async def stop(self) -> None:
        """Stop the background polling task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("hot_reload.stopped")

    async def _poll_loop(self) -> None:
        """Internal polling loop coroutine."""
        while self._running:
            try:
                reloaded = self.poll()
                if reloaded:
                    log.info("hot_reload.cycle_complete", reloaded=reloaded)
            except Exception as exc:  # noqa: BLE001
                log.error("hot_reload.poll_error", error=str(exc))
            await asyncio.sleep(self._poll_interval)

    # ---------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------- #

    @property
    def is_dev_mode(self) -> bool:
        """Return True if hot-reload is active (dev mode)."""
        return self._dev_mode

    def _require_dev_mode(self, operation: str) -> None:
        """Raise RuntimeError if not in dev mode."""
        if not self._dev_mode:
            raise RuntimeError(
                f"Hot-reload.{operation} is only available in dev mode. "
                "Set EAP_DEV_MODE=true or pass dev_mode=True to HotReloadWatcher()."
            )

    @staticmethod
    def _snapshot(directory: str) -> dict[str, float]:
        """Return a dict mapping relative file paths to their mtime."""
        snapshot: dict[str, float] = {}
        root = Path(directory)
        if not root.is_dir():
            return snapshot

        for path in root.rglob("*.py"):
            try:
                mtime = path.stat().st_mtime
                rel = str(path.relative_to(root))
                snapshot[rel] = mtime
            except OSError:
                pass

        return snapshot


# ------------------------------------------------------------------ #
# Module-level helpers
# ------------------------------------------------------------------ #


def _diff_snapshots(
    old: dict[str, float],
    new: dict[str, float],
) -> list[str]:
    """Return a list of files that were added, modified, or removed."""
    changed: list[str] = []

    for path, mtime in new.items():
        if path not in old or old[path] != mtime:
            changed.append(path)

    for path in old:
        if path not in new:
            changed.append(path)

    return changed


def _evict_modules(plugin_dir: str) -> None:
    """Remove any sys.modules entries that belong to the plugin directory."""
    plugin_dir_resolved = str(Path(plugin_dir).resolve())

    to_remove = [
        name
        for name, mod in list(sys.modules.items())
        if getattr(mod, "__file__", None) is not None
        and str(Path(getattr(mod, "__file__", "")).parent.resolve()).startswith(
            plugin_dir_resolved
        )
    ]

    for name in to_remove:
        del sys.modules[name]
        log.debug("hot_reload.module_evicted", module=name)
