"""Tests for __PLUGIN_CLASS__.

Phase 11B: Plugin Dev Kit - template test file.

These tests demonstrate the expected test patterns for EAP plugins.
Replace/extend them with tests for your specific plugin logic.

Run with:
    eap plugin test .
    # or directly:
    pytest tests/ -v
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import the plugin class - adjust path as needed
from plugin import __PLUGIN_CLASS__
from src.plugins.base import PluginContext, PluginHook, PluginMetadata


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def plugin() -> __PLUGIN_CLASS__:
    """Create a fresh plugin instance for each test."""
    return __PLUGIN_CLASS__()


@pytest.fixture
def plugin_context() -> PluginContext:
    """Create a test plugin context."""
    return PluginContext(
        tenant_id=uuid.uuid4(),
        settings={
            "debug": True,
            "api_key": "test-key",
        },
    )


# ------------------------------------------------------------------ #
# Metadata tests
# ------------------------------------------------------------------ #


class TestPluginMetadata:
    """Tests for plugin metadata correctness."""

    def test_metadata_returns_plugin_metadata(self, plugin: __PLUGIN_CLASS__) -> None:
        """Plugin metadata should be a PluginMetadata instance."""
        metadata = plugin.metadata
        assert isinstance(metadata, PluginMetadata)

    def test_metadata_name_is_not_empty(self, plugin: __PLUGIN_CLASS__) -> None:
        """Plugin name must not be empty."""
        assert plugin.metadata.name

    def test_metadata_version_follows_semver(self, plugin: __PLUGIN_CLASS__) -> None:
        """Plugin version must follow x.y.z semver format."""
        parts = plugin.metadata.version.split(".")
        assert len(parts) == 3, f"Expected x.y.z semver, got: {plugin.metadata.version}"
        assert all(p.isdigit() for p in parts), "All semver parts must be integers"

    def test_metadata_author_is_not_empty(self, plugin: __PLUGIN_CLASS__) -> None:
        """Plugin author must not be empty."""
        assert plugin.metadata.author

    def test_metadata_description_is_not_empty(self, plugin: __PLUGIN_CLASS__) -> None:
        """Plugin description must not be empty."""
        assert plugin.metadata.description


# ------------------------------------------------------------------ #
# Lifecycle tests
# ------------------------------------------------------------------ #


class TestPluginLifecycle:
    """Tests for plugin lifecycle hook correctness."""

    @pytest.mark.asyncio
    async def test_on_load_sets_initialized(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """on_load should set the plugin's initialized state."""
        assert not plugin._initialized
        await plugin.on_load(plugin_context)
        assert plugin._initialized

    @pytest.mark.asyncio
    async def test_on_load_stores_context(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """on_load should store the plugin context."""
        await plugin.on_load(plugin_context)
        assert plugin._context is plugin_context

    @pytest.mark.asyncio
    async def test_on_unload_clears_initialized(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """on_unload should clear the initialized state."""
        await plugin.on_load(plugin_context)
        await plugin.on_unload()
        assert not plugin._initialized

    @pytest.mark.asyncio
    async def test_on_unload_clears_context(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """on_unload should clear the stored context."""
        await plugin.on_load(plugin_context)
        await plugin.on_unload()
        assert plugin._context is None

    @pytest.mark.asyncio
    async def test_on_install_does_not_raise(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """on_install should complete without raising."""
        await plugin.on_install(plugin_context)

    @pytest.mark.asyncio
    async def test_on_uninstall_does_not_raise(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """on_uninstall should complete without raising."""
        await plugin.on_uninstall(plugin_context)


# ------------------------------------------------------------------ #
# Hook dispatch tests
# ------------------------------------------------------------------ #


class TestPluginHooks:
    """Tests for hook handler correctness."""

    @pytest.mark.asyncio
    async def test_handle_hook_before_agent_run_returns_dict(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """BEFORE_AGENT_RUN hook should return a dict."""
        await plugin.on_load(plugin_context)
        data: dict[str, Any] = {"conversation_id": str(uuid.uuid4())}
        result = await plugin.handle_hook(PluginHook.BEFORE_AGENT_RUN, data)
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_handle_hook_injects_plugin_context(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """BEFORE_AGENT_RUN hook should inject plugin_context into data."""
        await plugin.on_load(plugin_context)
        data: dict[str, Any] = {"conversation_id": str(uuid.uuid4())}
        result = await plugin.handle_hook(PluginHook.BEFORE_AGENT_RUN, data)
        assert "plugin_context" in result

    @pytest.mark.asyncio
    async def test_handle_hook_after_agent_run_passthrough(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """AFTER_AGENT_RUN hook should return data unchanged."""
        await plugin.on_load(plugin_context)
        data: dict[str, Any] = {"response": "Hello!", "tokens": 42}
        result = await plugin.handle_hook(PluginHook.AFTER_AGENT_RUN, data)
        assert result["response"] == "Hello!"
        assert result["tokens"] == 42

    @pytest.mark.asyncio
    async def test_handle_unknown_hook_passthrough(
        self,
        plugin: __PLUGIN_CLASS__,
        plugin_context: PluginContext,
    ) -> None:
        """Unhandled hooks should return data unchanged."""
        await plugin.on_load(plugin_context)
        data: dict[str, Any] = {"tool_name": "search", "args": {}}
        result = await plugin.handle_hook(PluginHook.BEFORE_TOOL_CALL, data)
        assert result == data


# ------------------------------------------------------------------ #
# Tool registration tests
# ------------------------------------------------------------------ #


class TestToolRegistration:
    """Tests for tool definition registration."""

    def test_register_tools_returns_list(self, plugin: __PLUGIN_CLASS__) -> None:
        """register_tools should return a list."""
        tools = plugin.register_tools()
        assert isinstance(tools, list)

    def test_each_tool_has_required_fields(self, plugin: __PLUGIN_CLASS__) -> None:
        """Each registered tool must have name, description, and parameters."""
        for tool in plugin.register_tools():
            assert "name" in tool, f"Tool missing 'name': {tool}"
            assert "description" in tool, f"Tool missing 'description': {tool}"
            assert "parameters" in tool, f"Tool missing 'parameters': {tool}"

    def test_tool_names_are_unique(self, plugin: __PLUGIN_CLASS__) -> None:
        """Tool names must be unique within a plugin."""
        tools = plugin.register_tools()
        names = [t["name"] for t in tools]
        assert len(names) == len(set(names)), "Duplicate tool names detected"
