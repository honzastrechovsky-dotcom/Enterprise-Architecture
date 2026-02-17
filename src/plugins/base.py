"""Base plugin classes and interfaces.

Defines the core plugin architecture:
- PluginMetadata: Plugin identification and requirements
- PluginHook: Lifecycle hook enumeration
- PluginContext: Runtime context passed to plugins
- BasePlugin: Abstract base class all plugins must implement
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog


class PluginHook(StrEnum):
    """Lifecycle hooks where plugins can intercept agent behavior."""

    BEFORE_AGENT_RUN = "before_agent_run"
    AFTER_AGENT_RUN = "after_agent_run"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    ON_ERROR = "on_error"
    ON_REASONING_STEP = "on_reasoning_step"


@dataclass
class PluginMetadata:
    """Metadata describing a plugin.

    This identifies the plugin and declares its requirements and compatibility.
    """

    name: str
    version: str
    author: str
    description: str
    required_permissions: list[str] = field(default_factory=list)
    compatible_versions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate metadata after initialization."""
        if not self.name:
            raise ValueError("Plugin name cannot be empty")
        if not self.version:
            raise ValueError("Plugin version cannot be empty")
        if not self.author:
            raise ValueError("Plugin author cannot be empty")
        if not self.description:
            raise ValueError("Plugin description cannot be empty")


@dataclass
class PluginContext:
    """Runtime context passed to plugins during lifecycle operations.

    Provides read-only access to settings and scoping information.
    """

    tenant_id: uuid.UUID
    settings: dict[str, Any]
    logger: structlog.BoundLogger | None = None

    def __post_init__(self) -> None:
        """Initialize logger if not provided."""
        if self.logger is None:
            self.logger = structlog.get_logger("plugin")


class BasePlugin(ABC):
    """Abstract base class for all plugins.

    All plugins must implement this interface. The plugin system uses this
    to manage plugin lifecycle and hook invocation.

    Subclasses must implement:
    - metadata: Property returning PluginMetadata
    - on_load: Called when plugin is loaded
    - on_unload: Called when plugin is unloaded
    - handle_hook: Called when a registered hook fires
    """

    @property
    @abstractmethod
    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        pass

    @abstractmethod
    async def on_load(self, context: PluginContext) -> None:
        """Called when the plugin is loaded.

        Use this to initialize resources, validate configuration, etc.

        Args:
            context: Plugin runtime context with tenant scope and settings

        Raises:
            Exception: If plugin cannot be loaded
        """
        pass

    @abstractmethod
    async def on_unload(self) -> None:
        """Called when the plugin is unloaded.

        Use this to clean up resources, close connections, etc.
        """
        pass

    @abstractmethod
    async def handle_hook(self, hook: PluginHook, data: dict[str, Any]) -> dict[str, Any]:
        """Handle a lifecycle hook invocation.

        Plugins can inspect and modify the data dict. The modified dict is
        passed to the next plugin in the chain, or back to the agent runtime.

        Args:
            hook: The hook type being invoked
            data: Hook-specific data dict (e.g., tool parameters, error info)

        Returns:
            Modified data dict (or original if no changes)
        """
        pass
