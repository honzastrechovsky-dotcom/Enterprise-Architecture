"""Plugin system for extending agent capabilities.

This module provides the plugin SDK for registering custom agent capabilities.
Plugins can add new tools, intercept agent lifecycle hooks, and extend
functionality in a tenant-scoped manner.

Core components:
- BasePlugin: Abstract base class for all plugins
- PluginMetadata: Plugin identification and requirements
- PluginHook: Lifecycle hook points
- PluginRegistry: Central registry for plugin discovery
- PluginLoader: Safe plugin loading with sandbox restrictions
"""

from src.plugins.base import (
    BasePlugin,
    PluginContext,
    PluginHook,
    PluginMetadata,
)
from src.plugins.registry import PluginRegistry, get_registry
from src.plugins.tool_plugin import (
    BaseToolPlugin,
    ToolContext,
    ToolDefinition,
    ToolResult,
)

__all__ = [
    "BasePlugin",
    "PluginContext",
    "PluginHook",
    "PluginMetadata",
    "PluginRegistry",
    "get_registry",
    "BaseToolPlugin",
    "ToolContext",
    "ToolDefinition",
    "ToolResult",
]
