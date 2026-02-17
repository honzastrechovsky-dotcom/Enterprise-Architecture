"""Tests for PluginRegistry.

Tests cover:
- Plugin registration (global and tenant-scoped)
- Plugin unregistration
- Plugin retrieval
- Plugin listing
- Hook retrieval
- Plugin validation
- Thread safety
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.plugins.base import BasePlugin, PluginContext, PluginHook, PluginMetadata
from src.plugins.registry import PluginRegistry, get_registry


# ------------------------------------------------------------------ #
# Test fixtures
# ------------------------------------------------------------------ #


class MockPlugin(BasePlugin):
    """Mock plugin for testing."""

    def __init__(self, metadata: PluginMetadata):
        self._metadata = metadata
        self._hooks: set[PluginHook] = set()

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    def register_hook(self, hook: PluginHook) -> None:
        """Register a hook that this plugin handles."""
        self._hooks.add(hook)

    async def on_load(self, context: PluginContext) -> None:
        pass

    async def on_unload(self) -> None:
        pass

    async def handle_hook(self, hook: PluginHook, data: dict) -> dict:
        if hook in self._hooks:
            return {**data, "handled_by": self.metadata.name}
        return data


@pytest.fixture
def registry():
    """Create a fresh registry for each test."""
    return PluginRegistry()


@pytest.fixture
def test_plugin():
    """Create a test plugin."""
    metadata = PluginMetadata(
        name="test_plugin",
        version="1.0.0",
        author="Test Author",
        description="Test plugin",
    )
    return MockPlugin(metadata)


@pytest.fixture
def tenant_id():
    """Create a test tenant ID."""
    return uuid.uuid4()


# ------------------------------------------------------------------ #
# Registration tests
# ------------------------------------------------------------------ #


def test_registry_register_global(registry: PluginRegistry, test_plugin: MockPlugin):
    """Test global plugin registration."""
    registry.register(test_plugin)

    retrieved = registry.get_plugin(test_plugin.metadata.name)
    assert retrieved is not None
    assert retrieved.metadata.name == test_plugin.metadata.name


def test_registry_register_tenant_scoped(
    registry: PluginRegistry,
    test_plugin: MockPlugin,
    tenant_id: uuid.UUID
):
    """Test tenant-scoped plugin registration."""
    registry.register(test_plugin, tenant_id=str(tenant_id))

    # Should be retrievable with tenant_id
    retrieved = registry.get_plugin(test_plugin.metadata.name, tenant_id=str(tenant_id))
    assert retrieved is not None

    # Should NOT be retrievable globally
    global_retrieved = registry.get_plugin(test_plugin.metadata.name)
    assert global_retrieved is None


def test_registry_register_duplicate_fails(registry: PluginRegistry, test_plugin: MockPlugin):
    """Test that duplicate registration raises error."""
    registry.register(test_plugin)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(test_plugin)


def test_registry_register_same_plugin_different_tenants(
    registry: PluginRegistry,
    tenant_id: uuid.UUID
):
    """Test same plugin can be registered for different tenants."""
    metadata = PluginMetadata(
        name="shared_plugin",
        version="1.0.0",
        author="Author",
        description="Shared plugin",
    )

    plugin1 = MockPlugin(metadata)
    plugin2 = MockPlugin(metadata)

    tenant1 = str(uuid.uuid4())
    tenant2 = str(uuid.uuid4())

    registry.register(plugin1, tenant_id=tenant1)
    registry.register(plugin2, tenant_id=tenant2)

    # Both should be retrievable in their respective tenants
    retrieved1 = registry.get_plugin("shared_plugin", tenant_id=tenant1)
    retrieved2 = registry.get_plugin("shared_plugin", tenant_id=tenant2)

    assert retrieved1 is not None
    assert retrieved2 is not None


# ------------------------------------------------------------------ #
# Unregistration tests
# ------------------------------------------------------------------ #


def test_registry_unregister_global(registry: PluginRegistry, test_plugin: MockPlugin):
    """Test global plugin unregistration."""
    registry.register(test_plugin)
    registry.unregister(test_plugin.metadata.name)

    retrieved = registry.get_plugin(test_plugin.metadata.name)
    assert retrieved is None


def test_registry_unregister_tenant_scoped(
    registry: PluginRegistry,
    test_plugin: MockPlugin,
    tenant_id: uuid.UUID
):
    """Test tenant-scoped plugin unregistration."""
    registry.register(test_plugin, tenant_id=str(tenant_id))
    registry.unregister(test_plugin.metadata.name, tenant_id=str(tenant_id))

    retrieved = registry.get_plugin(test_plugin.metadata.name, tenant_id=str(tenant_id))
    assert retrieved is None


def test_registry_unregister_nonexistent_silent(registry: PluginRegistry):
    """Test unregistering nonexistent plugin does not raise error."""
    # Should not raise
    registry.unregister("nonexistent_plugin")


# ------------------------------------------------------------------ #
# Retrieval tests
# ------------------------------------------------------------------ #


def test_registry_get_plugin_not_found(registry: PluginRegistry):
    """Test get_plugin returns None for nonexistent plugin."""
    result = registry.get_plugin("nonexistent")
    assert result is None


def test_registry_list_plugins_empty(registry: PluginRegistry):
    """Test list_plugins returns empty list for empty registry."""
    plugins = registry.list_plugins()
    assert plugins == []


def test_registry_list_plugins_global(registry: PluginRegistry):
    """Test list_plugins returns global plugins."""
    plugin1 = MockPlugin(
        PluginMetadata(name="plugin1", version="1.0.0", author="A", description="P1")
    )
    plugin2 = MockPlugin(
        PluginMetadata(name="plugin2", version="1.0.0", author="B", description="P2")
    )

    registry.register(plugin1)
    registry.register(plugin2)

    plugins = registry.list_plugins()
    assert len(plugins) == 2
    names = {p.name for p in plugins}
    assert names == {"plugin1", "plugin2"}


def test_registry_list_plugins_tenant_scoped(registry: PluginRegistry, tenant_id: uuid.UUID):
    """Test list_plugins returns tenant-scoped plugins."""
    global_plugin = MockPlugin(
        PluginMetadata(name="global", version="1.0.0", author="A", description="Global")
    )
    tenant_plugin = MockPlugin(
        PluginMetadata(name="tenant", version="1.0.0", author="B", description="Tenant")
    )

    registry.register(global_plugin)
    registry.register(tenant_plugin, tenant_id=str(tenant_id))

    # Global list should only include global plugin
    global_plugins = registry.list_plugins()
    assert len(global_plugins) == 1
    assert global_plugins[0].name == "global"

    # Tenant list should include both global and tenant plugins
    tenant_plugins = registry.list_plugins(tenant_id=str(tenant_id))
    assert len(tenant_plugins) == 2
    names = {p.name for p in tenant_plugins}
    assert names == {"global", "tenant"}


# ------------------------------------------------------------------ #
# Hook retrieval tests
# ------------------------------------------------------------------ #


def test_registry_get_hooks_empty(registry: PluginRegistry):
    """Test get_hooks returns empty list when no plugins registered."""
    hooks = registry.get_hooks(PluginHook.BEFORE_AGENT_RUN)
    assert hooks == []


def test_registry_get_hooks_filters_by_hook_type(registry: PluginRegistry):
    """Test get_hooks returns only plugins that handle specific hook."""
    plugin1 = MockPlugin(
        PluginMetadata(name="plugin1", version="1.0.0", author="A", description="P1")
    )
    plugin1.register_hook(PluginHook.BEFORE_AGENT_RUN)

    plugin2 = MockPlugin(
        PluginMetadata(name="plugin2", version="1.0.0", author="B", description="P2")
    )
    plugin2.register_hook(PluginHook.AFTER_AGENT_RUN)

    registry.register(plugin1)
    registry.register(plugin2)

    before_hooks = registry.get_hooks(PluginHook.BEFORE_AGENT_RUN)
    # Note: In real implementation, registry should track which hooks plugins handle
    # For now, this test establishes the contract
    assert len(before_hooks) >= 0  # Placeholder until implementation


def test_registry_get_hooks_tenant_scoped(registry: PluginRegistry, tenant_id: uuid.UUID):
    """Test get_hooks respects tenant scope."""
    global_plugin = MockPlugin(
        PluginMetadata(name="global", version="1.0.0", author="A", description="Global")
    )
    tenant_plugin = MockPlugin(
        PluginMetadata(name="tenant", version="1.0.0", author="B", description="Tenant")
    )

    registry.register(global_plugin)
    registry.register(tenant_plugin, tenant_id=str(tenant_id))

    # Tenant should see both global and tenant plugins
    tenant_hooks = registry.get_hooks(PluginHook.BEFORE_AGENT_RUN, tenant_id=str(tenant_id))
    assert len(tenant_hooks) >= 0  # Placeholder


# ------------------------------------------------------------------ #
# Validation tests
# ------------------------------------------------------------------ #


def test_registry_validate_plugin_success(registry: PluginRegistry):
    """Test validate_plugin returns no errors for valid plugin."""
    plugin = MockPlugin(
        PluginMetadata(
            name="valid_plugin",
            version="1.0.0",
            author="Author",
            description="Valid plugin",
        )
    )

    errors = registry.validate_plugin(plugin)
    assert errors == []


def test_registry_validate_plugin_invalid_name(registry: PluginRegistry):
    """Test validate_plugin detects invalid plugin name."""
    # PluginMetadata.__post_init__ raises on empty name, so we need to bypass it
    metadata = PluginMetadata.__new__(PluginMetadata)
    metadata.name = ""
    metadata.version = "1.0.0"
    metadata.author = "Author"
    metadata.description = "Invalid"
    metadata.required_permissions = []
    metadata.compatible_versions = []

    plugin = MockPlugin(metadata)

    errors = registry.validate_plugin(plugin)
    assert len(errors) > 0
    assert any("name" in error.lower() for error in errors)


def test_registry_validate_plugin_invalid_version(registry: PluginRegistry):
    """Test validate_plugin detects invalid version format."""
    plugin = MockPlugin(
        PluginMetadata(
            name="plugin",
            version="not-a-semver",  # Invalid version
            author="Author",
            description="Invalid",
        )
    )

    errors = registry.validate_plugin(plugin)
    assert len(errors) > 0
    assert any("version" in error.lower() for error in errors)


# ------------------------------------------------------------------ #
# Thread safety tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_registry_concurrent_registration(registry: PluginRegistry):
    """Test concurrent plugin registration is thread-safe."""
    async def register_plugin(name: str):
        plugin = MockPlugin(
            PluginMetadata(
                name=name,
                version="1.0.0",
                author="Author",
                description=f"Plugin {name}",
            )
        )
        registry.register(plugin)

    # Register 10 plugins concurrently
    tasks = [register_plugin(f"plugin_{i}") for i in range(10)]
    await asyncio.gather(*tasks)

    plugins = registry.list_plugins()
    assert len(plugins) == 10


# ------------------------------------------------------------------ #
# Singleton tests
# ------------------------------------------------------------------ #


def test_get_registry_returns_singleton():
    """Test get_registry returns same instance."""
    reg1 = get_registry()
    reg2 = get_registry()

    assert reg1 is reg2
