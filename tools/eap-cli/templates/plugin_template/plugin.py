"""__PLUGIN_CLASS__ - main plugin implementation.

This file is the entry point for the __PLUGIN_NAME__ plugin.
Implement the abstract methods from BasePlugin to define plugin behaviour.

Lifecycle:
  1. on_install  - Called once when the plugin is first installed for a tenant
  2. on_load     - Called every time the plugin is loaded into the runtime
  3. handle_hook - Called for each registered lifecycle hook event
  4. on_unload   - Called when the plugin is being unloaded or disabled
  5. on_uninstall - Called once when the plugin is permanently removed
"""

from __future__ import annotations

from typing import Any

import structlog

from src.plugins.base import BasePlugin, PluginContext, PluginHook, PluginMetadata

log = structlog.get_logger(__name__)


class __PLUGIN_CLASS__(BasePlugin):
    """__PLUGIN_DESCRIPTION__

    Replace this class body with your implementation.
    """

    def __init__(self) -> None:
        self._context: PluginContext | None = None
        self._initialized = False

    # ---------------------------------------------------------------- #
    # Metadata
    # ---------------------------------------------------------------- #

    @property
    def metadata(self) -> PluginMetadata:
        """Return plugin metadata.  This must match plugin.yaml."""
        return PluginMetadata(
            name="__PLUGIN_NAME__",
            version="0.1.0",
            author="Your Name",
            description="__PLUGIN_DESCRIPTION__",
            required_permissions=["agent.read"],
            compatible_versions=["0.1.0"],
        )

    # ---------------------------------------------------------------- #
    # Lifecycle hooks
    # ---------------------------------------------------------------- #

    async def on_load(self, context: PluginContext) -> None:
        """Called when the plugin is loaded into the platform runtime.

        Initialize any resources needed by the plugin here.
        This is called every time the plugin is activated (e.g., after restart).

        Args:
            context: Runtime context with tenant_id and settings.
        """
        self._context = context
        self._initialized = True
        log.info(
            "plugin.loaded",
            plugin="__PLUGIN_NAME__",
            tenant_id=str(context.tenant_id),
        )

    async def on_unload(self) -> None:
        """Called when the plugin is being unloaded.

        Clean up resources (connections, file handles, etc.) here.
        """
        self._initialized = False
        self._context = None
        log.info("plugin.unloaded", plugin="__PLUGIN_NAME__")

    async def handle_hook(
        self, hook: PluginHook, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle a lifecycle hook invocation.

        Inspect or modify the data dict for the given hook type.
        Return the (possibly modified) data dict.

        Args:
            hook: Which hook is firing (e.g., PluginHook.BEFORE_AGENT_RUN)
            data: Hook-specific data. Modify and return to pass to next plugin.

        Returns:
            The (possibly modified) data dict.
        """
        if hook == PluginHook.BEFORE_AGENT_RUN:
            return await self._on_before_agent_run(data)
        elif hook == PluginHook.AFTER_AGENT_RUN:
            return await self._on_after_agent_run(data)
        # Pass through unhandled hooks unchanged
        return data

    # ---------------------------------------------------------------- #
    # Install / uninstall hooks
    # ---------------------------------------------------------------- #

    async def on_install(self, context: PluginContext) -> None:
        """Called once when the plugin is first installed for a tenant.

        Use this for one-time setup: creating configuration defaults,
        provisioning external resources, etc.

        Args:
            context: Runtime context with tenant_id and settings.
        """
        log.info(
            "plugin.installed",
            plugin="__PLUGIN_NAME__",
            tenant_id=str(context.tenant_id),
        )

    async def on_uninstall(self, context: PluginContext) -> None:
        """Called once when the plugin is permanently removed.

        Clean up any persistent state created during on_install.

        Args:
            context: Runtime context with tenant_id and settings.
        """
        log.info(
            "plugin.uninstalled",
            plugin="__PLUGIN_NAME__",
            tenant_id=str(context.tenant_id),
        )

    # ---------------------------------------------------------------- #
    # Tool registration
    # ---------------------------------------------------------------- #

    def register_tools(self) -> list[dict[str, Any]]:
        """Return a list of tool definitions exposed by this plugin.

        Each tool definition is a dict with:
          - name:        str - Unique tool identifier
          - description: str - What the tool does (shown to the LLM)
          - parameters:  dict - JSON Schema for tool parameters

        Returns:
            List of tool definition dicts.
        """
        return [
            {
                "name": "__PLUGIN_NAME___example_tool",
                "description": "An example tool provided by __PLUGIN_NAME__.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The input query for the tool.",
                        },
                    },
                    "required": ["query"],
                },
            }
        ]

    # ---------------------------------------------------------------- #
    # Private hook implementations
    # ---------------------------------------------------------------- #

    async def _on_before_agent_run(self, data: dict[str, Any]) -> dict[str, Any]:
        """Pre-process data before an agent run starts."""
        log.debug(
            "plugin.before_agent_run",
            plugin="__PLUGIN_NAME__",
            conversation_id=data.get("conversation_id"),
        )
        # Example: add a custom context value
        data.setdefault("plugin_context", {})
        data["plugin_context"]["__PLUGIN_NAME__"] = {"injected": True}
        return data

    async def _on_after_agent_run(self, data: dict[str, Any]) -> dict[str, Any]:
        """Post-process data after an agent run completes."""
        log.debug(
            "plugin.after_agent_run",
            plugin="__PLUGIN_NAME__",
            conversation_id=data.get("conversation_id"),
        )
        return data
