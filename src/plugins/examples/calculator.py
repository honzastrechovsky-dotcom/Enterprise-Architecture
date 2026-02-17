"""Example calculator tool plugin.

Demonstrates how to create a tool plugin that adds custom capabilities
to the agent platform. This plugin provides basic arithmetic operations.
"""

from __future__ import annotations

from typing import Any

import structlog

from src.plugins.base import PluginContext, PluginMetadata
from src.plugins.tool_plugin import (
    BaseToolPlugin,
    ToolContext,
    ToolDefinition,
    ToolResult,
)

log = structlog.get_logger(__name__)


class CalculatorPlugin(BaseToolPlugin):
    """Example plugin that provides arithmetic calculation tools.

    This demonstrates:
    - Tool definition with JSON Schema parameters
    - Tool execution with parameter validation
    - Error handling
    - Result formatting
    """

    def __init__(self):
        """Initialize calculator plugin."""
        self._metadata = PluginMetadata(
            name="calculator",
            version="1.0.0",
            author="Enterprise Agent Platform",
            description="Provides basic arithmetic calculation tools",
            required_permissions=["math:calculate"],
            compatible_versions=["0.1.0"],
        )

        self._tools = [
            ToolDefinition(
                name="add",
                description="Add two numbers",
                parameters={
                    "type": "object",
                    "properties": {
                        "a": {
                            "type": "number",
                            "description": "First number",
                        },
                        "b": {
                            "type": "number",
                            "description": "Second number",
                        },
                    },
                    "required": ["a", "b"],
                },
            ),
            ToolDefinition(
                name="subtract",
                description="Subtract second number from first",
                parameters={
                    "type": "object",
                    "properties": {
                        "a": {
                            "type": "number",
                            "description": "First number",
                        },
                        "b": {
                            "type": "number",
                            "description": "Second number",
                        },
                    },
                    "required": ["a", "b"],
                },
            ),
            ToolDefinition(
                name="multiply",
                description="Multiply two numbers",
                parameters={
                    "type": "object",
                    "properties": {
                        "a": {
                            "type": "number",
                            "description": "First number",
                        },
                        "b": {
                            "type": "number",
                            "description": "Second number",
                        },
                    },
                    "required": ["a", "b"],
                },
            ),
            ToolDefinition(
                name="divide",
                description="Divide first number by second",
                parameters={
                    "type": "object",
                    "properties": {
                        "a": {
                            "type": "number",
                            "description": "Numerator",
                        },
                        "b": {
                            "type": "number",
                            "description": "Denominator (cannot be zero)",
                        },
                    },
                    "required": ["a", "b"],
                },
            ),
        ]

    @property
    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return self._metadata

    @property
    def tools(self) -> list[ToolDefinition]:
        """Return list of tools this plugin provides."""
        return self._tools

    async def on_load(self, context: PluginContext) -> None:
        """Called when plugin is loaded."""
        log.info(
            "calculator.plugin_loaded",
            tenant_id=str(context.tenant_id),
            plugin_version=self.metadata.version,
        )

    async def on_unload(self) -> None:
        """Called when plugin is unloaded."""
        log.info("calculator.plugin_unloaded")

    async def execute(
        self,
        tool_name: str,
        params: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute a calculator tool.

        Args:
            tool_name: Name of tool to execute
            params: Tool parameters
            context: Execution context

        Returns:
            ToolResult with calculation result or error
        """
        log.info(
            "calculator.tool_execute",
            tool_name=tool_name,
            params=params,
            tenant_id=str(context.tenant_id),
        )

        try:
            # Extract parameters
            a = params.get("a")
            b = params.get("b")

            if a is None or b is None:
                return ToolResult(
                    success=False,
                    error="Missing required parameters: a and b",
                )

            # Execute operation
            if tool_name == "add":
                result = a + b
            elif tool_name == "subtract":
                result = a - b
            elif tool_name == "multiply":
                result = a * b
            elif tool_name == "divide":
                if b == 0:
                    return ToolResult(
                        success=False,
                        error="Division by zero is not allowed",
                    )
                result = a / b
            else:
                return ToolResult(
                    success=False,
                    error=f"Unknown tool: {tool_name}",
                )

            return ToolResult(
                success=True,
                data={"result": result},
                metadata={
                    "operation": tool_name,
                    "operands": {"a": a, "b": b},
                },
            )

        except Exception as e:
            log.error(
                "calculator.tool_error",
                tool_name=tool_name,
                error=str(e),
            )
            return ToolResult(
                success=False,
                error=f"Calculation error: {str(e)}",
            )
