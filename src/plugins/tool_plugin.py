"""Tool plugin base classes.

Extends the plugin system to support custom tool registration.
Tool plugins can add new capabilities that agents can invoke during execution.
"""

from __future__ import annotations

import uuid
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.plugins.base import BasePlugin, PluginHook


@dataclass
class ToolDefinition:
    """Defines a tool that a plugin provides.

    Tools are functions that agents can call during execution. Each tool
    has a name, description, and JSON Schema describing its parameters.
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema dict
    required_permissions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate tool definition."""
        if not self.name:
            raise ValueError("Tool name cannot be empty")
        if not self.description:
            raise ValueError("Tool description cannot be empty")
        if not isinstance(self.parameters, dict):
            raise ValueError("Tool parameters must be a JSON Schema dict")


@dataclass
class ToolResult:
    """Result of a tool execution.

    Contains the execution outcome, data, and any error information.
    """

    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolContext:
    """Context passed to tool execution.

    Provides scope and tracing information for the tool invocation.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    trace_id: uuid.UUID | None = None


class BaseToolPlugin(BasePlugin):
    """Abstract base class for tool plugins.

    Tool plugins extend the agent's capabilities by registering new tools
    that can be invoked during agent execution.

    Subclasses must implement:
    - All BasePlugin abstract methods
    - tools: Property returning list of ToolDefinitions
    - execute: Async method to execute a tool
    """

    @property
    @abstractmethod
    def tools(self) -> list[ToolDefinition]:
        """Return the list of tools this plugin provides."""
        pass

    @abstractmethod
    async def execute(
        self,
        tool_name: str,
        params: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute a tool with given parameters.

        Args:
            tool_name: Name of the tool to execute
            params: Tool parameters (validated against tool definition)
            context: Execution context with tenant scope and tracing

        Returns:
            ToolResult with execution outcome

        Raises:
            ValueError: If tool_name is not recognized
        """
        pass

    async def handle_hook(self, hook: PluginHook, data: dict[str, Any]) -> dict[str, Any]:
        """Default hook handler for tool plugins.

        Tool plugins typically don't need to handle hooks unless they want to
        intercept tool calls or agent lifecycle events.

        This default implementation is a pass-through.
        """
        return data
