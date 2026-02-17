"""Tests for Plugin base classes and metadata.

Tests cover:
- PluginMetadata creation and validation
- PluginHook enum values
- BasePlugin abstract class interface
- PluginContext dataclass
- Plugin lifecycle: on_load, on_unload, handle_hook
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from src.plugins.base import (
    BasePlugin,
    PluginContext,
    PluginHook,
    PluginMetadata,
)


# ------------------------------------------------------------------ #
# PluginMetadata tests
# ------------------------------------------------------------------ #


def test_plugin_metadata_creation_success():
    """Test PluginMetadata creation with valid fields."""
    metadata = PluginMetadata(
        name="test_plugin",
        version="1.0.0",
        author="Test Author",
        description="A test plugin",
        required_permissions=["read", "write"],
        compatible_versions=["0.1.0", "0.2.0"],
    )

    assert metadata.name == "test_plugin"
    assert metadata.version == "1.0.0"
    assert metadata.author == "Test Author"
    assert metadata.description == "A test plugin"
    assert metadata.required_permissions == ["read", "write"]
    assert metadata.compatible_versions == ["0.1.0", "0.2.0"]


def test_plugin_metadata_minimal():
    """Test PluginMetadata with minimal required fields."""
    metadata = PluginMetadata(
        name="minimal_plugin",
        version="1.0.0",
        author="Minimal Author",
        description="Minimal description",
    )

    assert metadata.name == "minimal_plugin"
    assert metadata.required_permissions == []  # Default
    assert metadata.compatible_versions == []  # Default


# ------------------------------------------------------------------ #
# PluginHook enum tests
# ------------------------------------------------------------------ #


def test_plugin_hook_enum_values():
    """Test that all expected PluginHook values exist."""
    assert PluginHook.BEFORE_AGENT_RUN
    assert PluginHook.AFTER_AGENT_RUN
    assert PluginHook.BEFORE_TOOL_CALL
    assert PluginHook.AFTER_TOOL_CALL
    assert PluginHook.ON_ERROR
    assert PluginHook.ON_REASONING_STEP


def test_plugin_hook_enum_string_values():
    """Test PluginHook enum string representations."""
    assert PluginHook.BEFORE_AGENT_RUN.value == "before_agent_run"
    assert PluginHook.AFTER_AGENT_RUN.value == "after_agent_run"
    assert PluginHook.BEFORE_TOOL_CALL.value == "before_tool_call"
    assert PluginHook.AFTER_TOOL_CALL.value == "after_tool_call"
    assert PluginHook.ON_ERROR.value == "on_error"
    assert PluginHook.ON_REASONING_STEP.value == "on_reasoning_step"


# ------------------------------------------------------------------ #
# PluginContext tests
# ------------------------------------------------------------------ #


def test_plugin_context_creation():
    """Test PluginContext creation."""
    tenant_id = uuid.uuid4()
    settings = {"key": "value"}

    context = PluginContext(
        tenant_id=tenant_id,
        settings=settings,
        logger=None,  # Will be replaced with actual logger
    )

    assert context.tenant_id == tenant_id
    assert context.settings == settings


# ------------------------------------------------------------------ #
# BasePlugin tests
# ------------------------------------------------------------------ #


class ConcreteTestPlugin(BasePlugin):
    """Concrete implementation for testing BasePlugin."""

    def __init__(self, metadata: PluginMetadata):
        self._metadata = metadata
        self._loaded = False
        self._hook_calls: list[tuple[PluginHook, dict[str, Any]]] = []

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    async def on_load(self, context: PluginContext) -> None:
        """Mark plugin as loaded."""
        self._loaded = True

    async def on_unload(self) -> None:
        """Mark plugin as unloaded."""
        self._loaded = False

    async def handle_hook(self, hook: PluginHook, data: dict[str, Any]) -> dict[str, Any]:
        """Record hook call and return modified data."""
        self._hook_calls.append((hook, data))
        return {**data, "processed": True}

    def is_loaded(self) -> bool:
        """Check if plugin is loaded."""
        return self._loaded

    def get_hook_calls(self) -> list[tuple[PluginHook, dict[str, Any]]]:
        """Get all recorded hook calls."""
        return self._hook_calls


@pytest.fixture
def test_metadata():
    """Create test metadata."""
    return PluginMetadata(
        name="test_plugin",
        version="1.0.0",
        author="Test Author",
        description="A test plugin",
    )


@pytest.fixture
def test_plugin(test_metadata: PluginMetadata):
    """Create test plugin instance."""
    return ConcreteTestPlugin(test_metadata)


@pytest.fixture
def test_context():
    """Create test plugin context."""
    return PluginContext(
        tenant_id=uuid.uuid4(),
        settings={"test_setting": "value"},
        logger=None,
    )


@pytest.mark.asyncio
async def test_base_plugin_lifecycle(test_plugin: ConcreteTestPlugin, test_context: PluginContext):
    """Test plugin lifecycle: load -> use -> unload."""
    # Initially not loaded
    assert not test_plugin.is_loaded()

    # Load plugin
    await test_plugin.on_load(test_context)
    assert test_plugin.is_loaded()

    # Use plugin
    result = await test_plugin.handle_hook(
        PluginHook.BEFORE_AGENT_RUN,
        {"key": "value"}
    )
    assert result["processed"] is True
    assert result["key"] == "value"

    # Unload plugin
    await test_plugin.on_unload()
    assert not test_plugin.is_loaded()


@pytest.mark.asyncio
async def test_base_plugin_handle_multiple_hooks(test_plugin: ConcreteTestPlugin, test_context: PluginContext):
    """Test plugin handles multiple hook types."""
    await test_plugin.on_load(test_context)

    # Call different hooks
    await test_plugin.handle_hook(PluginHook.BEFORE_AGENT_RUN, {"step": 1})
    await test_plugin.handle_hook(PluginHook.AFTER_AGENT_RUN, {"step": 2})
    await test_plugin.handle_hook(PluginHook.ON_ERROR, {"error": "test"})

    hook_calls = test_plugin.get_hook_calls()
    assert len(hook_calls) == 3
    assert hook_calls[0][0] == PluginHook.BEFORE_AGENT_RUN
    assert hook_calls[1][0] == PluginHook.AFTER_AGENT_RUN
    assert hook_calls[2][0] == PluginHook.ON_ERROR


@pytest.mark.asyncio
async def test_base_plugin_metadata_access(test_plugin: ConcreteTestPlugin, test_metadata: PluginMetadata):
    """Test plugin metadata is accessible."""
    assert test_plugin.metadata.name == test_metadata.name
    assert test_plugin.metadata.version == test_metadata.version
    assert test_plugin.metadata.author == test_metadata.author
