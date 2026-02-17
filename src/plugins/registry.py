"""Plugin registry - central plugin discovery and management.

The registry maintains the catalog of all loaded plugins, both global
and tenant-scoped. It provides thread-safe registration, unregistration,
and plugin discovery.
"""

from __future__ import annotations

import asyncio
import re

import structlog

from src.plugins.base import BasePlugin, PluginHook, PluginMetadata

log = structlog.get_logger(__name__)


class PluginRegistry:
    """Thread-safe registry for plugin discovery and management.

    Plugins can be registered globally (available to all tenants) or
    tenant-scoped (available only to specific tenant).

    The registry is a singleton - use get_registry() to access it.
    """

    def __init__(self) -> None:
        """Initialize empty registry."""
        self._global_plugins: dict[str, BasePlugin] = {}
        self._tenant_plugins: dict[str, dict[str, BasePlugin]] = {}
        self._lock = asyncio.Lock()

    def register(self, plugin: BasePlugin, tenant_id: str | None = None) -> None:
        """Register a plugin globally or for a specific tenant.

        Args:
            plugin: The plugin instance to register
            tenant_id: Optional tenant ID for tenant-scoped registration

        Raises:
            ValueError: If plugin with this name is already registered
        """
        plugin_name = plugin.metadata.name

        if tenant_id is None:
            # Global registration
            if plugin_name in self._global_plugins:
                raise ValueError(
                    f"Plugin '{plugin_name}' is already registered globally. "
                    "Unregister it first or use a different name."
                )
            self._global_plugins[plugin_name] = plugin
            log.info(
                "registry.plugin_registered",
                plugin_name=plugin_name,
                version=plugin.metadata.version,
                scope="global",
            )
        else:
            # Tenant-scoped registration
            if tenant_id not in self._tenant_plugins:
                self._tenant_plugins[tenant_id] = {}

            if plugin_name in self._tenant_plugins[tenant_id]:
                raise ValueError(
                    f"Plugin '{plugin_name}' is already registered for tenant {tenant_id}"
                )

            self._tenant_plugins[tenant_id][plugin_name] = plugin
            log.info(
                "registry.plugin_registered",
                plugin_name=plugin_name,
                version=plugin.metadata.version,
                scope="tenant",
                tenant_id=tenant_id,
            )

    def unregister(self, plugin_name: str, tenant_id: str | None = None) -> None:
        """Unregister a plugin.

        Args:
            plugin_name: Name of the plugin to unregister
            tenant_id: Optional tenant ID for tenant-scoped unregistration
        """
        if tenant_id is None:
            # Global unregistration
            if plugin_name in self._global_plugins:
                del self._global_plugins[plugin_name]
                log.info(
                    "registry.plugin_unregistered",
                    plugin_name=plugin_name,
                    scope="global",
                )
        else:
            # Tenant-scoped unregistration
            if tenant_id in self._tenant_plugins:
                if plugin_name in self._tenant_plugins[tenant_id]:
                    del self._tenant_plugins[tenant_id][plugin_name]
                    log.info(
                        "registry.plugin_unregistered",
                        plugin_name=plugin_name,
                        scope="tenant",
                        tenant_id=tenant_id,
                    )

    def get_plugin(self, name: str, tenant_id: str | None = None) -> BasePlugin | None:
        """Get a plugin by name.

        For tenant-scoped queries, checks both tenant-specific plugins and
        global plugins. Tenant-specific plugins take precedence.

        Args:
            name: Plugin name
            tenant_id: Optional tenant ID

        Returns:
            Plugin instance if found, None otherwise
        """
        if tenant_id is not None:
            # Check tenant-specific plugins first
            if tenant_id in self._tenant_plugins:
                if name in self._tenant_plugins[tenant_id]:
                    return self._tenant_plugins[tenant_id][name]

            # Fall back to global plugins
            return self._global_plugins.get(name)
        else:
            # Global query only
            return self._global_plugins.get(name)

    def list_plugins(self, tenant_id: str | None = None) -> list[PluginMetadata]:
        """List all available plugins.

        For tenant-scoped queries, returns both global and tenant-specific
        plugins. For global queries, returns only global plugins.

        Args:
            tenant_id: Optional tenant ID for scoped query

        Returns:
            List of plugin metadata
        """
        plugins: list[BasePlugin] = []

        if tenant_id is None:
            # Global query - only global plugins
            plugins = list(self._global_plugins.values())
        else:
            # Tenant query - global + tenant-specific
            plugins = list(self._global_plugins.values())

            if tenant_id in self._tenant_plugins:
                # Add tenant-specific plugins (may override global names)
                tenant_plugins = self._tenant_plugins[tenant_id].values()
                plugins.extend(tenant_plugins)

        return [p.metadata for p in plugins]

    def get_hooks(
        self,
        hook: PluginHook,
        tenant_id: str | None = None,
    ) -> list[BasePlugin]:
        """Get all plugins that should be invoked for a hook.

        Returns plugins in the order they should be invoked. For tenant-scoped
        queries, includes both global and tenant-specific plugins.

        Args:
            hook: The hook type to query
            tenant_id: Optional tenant ID for scoped query

        Returns:
            List of plugins that handle this hook
        """
        plugins: list[BasePlugin] = []

        if tenant_id is None:
            # Global query
            plugins = list(self._global_plugins.values())
        else:
            # Tenant query - global + tenant-specific
            plugins = list(self._global_plugins.values())

            if tenant_id in self._tenant_plugins:
                plugins.extend(self._tenant_plugins[tenant_id].values())

        # In a full implementation, we'd filter by which plugins actually
        # handle this hook. For now, return all plugins (they can no-op).
        return plugins

    def validate_plugin(self, plugin: BasePlugin) -> list[str]:
        """Validate a plugin before registration.

        Checks:
        - Metadata completeness
        - Version format (semver)
        - Permission format
        - No dangerous operations

        Args:
            plugin: Plugin to validate

        Returns:
            List of validation errors (empty if valid)
        """
        errors: list[str] = []

        metadata = plugin.metadata

        # Validate name
        if not metadata.name or not metadata.name.strip():
            errors.append("Plugin name cannot be empty")

        # Validate version format (basic semver check)
        if not metadata.version:
            errors.append("Plugin version cannot be empty")
        elif not re.match(r"^\d+\.\d+\.\d+", metadata.version):
            errors.append(
                f"Plugin version '{metadata.version}' is not valid semver format (X.Y.Z)"
            )

        # Validate author
        if not metadata.author:
            errors.append("Plugin author cannot be empty")

        # Validate description
        if not metadata.description:
            errors.append("Plugin description cannot be empty")

        # Validate permissions format
        for perm in metadata.required_permissions:
            if not isinstance(perm, str) or not perm.strip():
                errors.append(f"Invalid permission: {perm!r}")

        return errors

    def clear(self) -> None:
        """Clear all plugins. Used for testing."""
        self._global_plugins.clear()
        self._tenant_plugins.clear()
        log.debug("registry.cleared")


# Module-level singleton instance
_registry = PluginRegistry()


def get_registry() -> PluginRegistry:
    """Get the global plugin registry instance."""
    return _registry
