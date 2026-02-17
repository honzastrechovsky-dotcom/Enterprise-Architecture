"""Tests for ToolPlugin classes.

Tests cover:
- ToolDefinition creation and validation
- ToolResult dataclass
- ToolContext dataclass
- BaseToolPlugin abstract class
- Tool execution with parameters
"""

from __future__ import annotations

import uuid

import pytest

from src.plugins.tool_plugin import (
    BaseToolPlugin,
    ToolContext,
    ToolDefinition,
    ToolResult,
)
from src.plugins.base import PluginMetadata, PluginContext


# ------------------------------------------------------------------ #
# ToolDefinition tests
# ------------------------------------------------------------------ #


def test_tool_definition_creation_success():
    """Test ToolDefinition creation with valid fields."""
    tool_def = ToolDefinition(
        name="calculator",
        description="Performs arithmetic calculations",
        parameters={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["add", "subtract"]},
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["operation", "a", "b"],
        },
        required_permissions=["math:calculate"],
    )

    assert tool_def.name == "calculator"
    assert tool_def.description == "Performs arithmetic calculations"
    assert "operation" in tool_def.parameters["properties"]
    assert tool_def.required_permissions == ["math:calculate"]


def test_tool_definition_minimal():
    """Test ToolDefinition with minimal fields."""
    tool_def = ToolDefinition(
        name="simple_tool",
        description="A simple tool",
        parameters={"type": "object", "properties": {}},
    )

    assert tool_def.name == "simple_tool"
    assert tool_def.required_permissions == []  # Default


# ------------------------------------------------------------------ #
# ToolResult tests
# ------------------------------------------------------------------ #


def test_tool_result_success():
    """Test ToolResult for successful execution."""
    result = ToolResult(
        success=True,
        data={"result": 42},
        error=None,
        metadata={"execution_time_ms": 5},
    )

    assert result.success is True
    assert result.data == {"result": 42}
    assert result.error is None
    assert result.metadata["execution_time_ms"] == 5


def test_tool_result_failure():
    """Test ToolResult for failed execution."""
    result = ToolResult(
        success=False,
        data=None,
        error="Division by zero",
        metadata={},
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Division by zero"


def test_tool_result_defaults():
    """Test ToolResult with default values."""
    result = ToolResult(
        success=True,
        data={"value": 100},
    )

    assert result.success is True
    assert result.error is None
    assert result.metadata == {}


# ------------------------------------------------------------------ #
# ToolContext tests
# ------------------------------------------------------------------ #


def test_tool_context_creation():
    """Test ToolContext creation with all fields."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    trace_id = uuid.uuid4()

    context = ToolContext(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        trace_id=trace_id,
    )

    assert context.tenant_id == tenant_id
    assert context.user_id == user_id
    assert context.conversation_id == conversation_id
    assert context.trace_id == trace_id


def test_tool_context_optional_fields():
    """Test ToolContext with optional fields as None."""
    tenant_id = uuid.uuid4()

    context = ToolContext(
        tenant_id=tenant_id,
        user_id=None,
        conversation_id=None,
        trace_id=None,
    )

    assert context.tenant_id == tenant_id
    assert context.user_id is None
    assert context.conversation_id is None
    assert context.trace_id is None


# ------------------------------------------------------------------ #
# BaseToolPlugin tests
# ------------------------------------------------------------------ #


class ConcreteToolPlugin(BaseToolPlugin):
    """Concrete implementation for testing BaseToolPlugin."""

    def __init__(self, metadata: PluginMetadata, tools: list[ToolDefinition]):
        self._metadata = metadata
        self._tools = tools
        self._executions: list[tuple[str, dict, ToolContext]] = []

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    @property
    def tools(self) -> list[ToolDefinition]:
        return self._tools

    async def on_load(self, context: PluginContext) -> None:
        """No-op for testing."""
        pass

    async def on_unload(self) -> None:
        """No-op for testing."""
        pass

    async def execute(
        self,
        tool_name: str,
        params: dict,
        context: ToolContext,
    ) -> ToolResult:
        """Record execution and return mock result."""
        self._executions.append((tool_name, params, context))

        if tool_name == "add":
            return ToolResult(
                success=True,
                data={"result": params["a"] + params["b"]},
            )
        elif tool_name == "error_tool":
            return ToolResult(
                success=False,
                error="Simulated error",
            )
        else:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}",
            )

    def get_executions(self) -> list[tuple[str, dict, ToolContext]]:
        """Get recorded executions."""
        return self._executions


@pytest.fixture
def tool_metadata():
    """Create metadata for tool plugin."""
    return PluginMetadata(
        name="calculator_plugin",
        version="1.0.0",
        author="Test Author",
        description="Calculator tool plugin",
    )


@pytest.fixture
def tool_definitions():
    """Create tool definitions."""
    return [
        ToolDefinition(
            name="add",
            description="Add two numbers",
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        ),
        ToolDefinition(
            name="error_tool",
            description="A tool that always fails",
            parameters={"type": "object", "properties": {}},
        ),
    ]


@pytest.fixture
def tool_plugin(tool_metadata: PluginMetadata, tool_definitions: list[ToolDefinition]):
    """Create tool plugin instance."""
    return ConcreteToolPlugin(tool_metadata, tool_definitions)


@pytest.fixture
def tool_context():
    """Create tool execution context."""
    return ToolContext(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_tool_plugin_execute_success(tool_plugin: ConcreteToolPlugin, tool_context: ToolContext):
    """Test successful tool execution."""
    result = await tool_plugin.execute(
        "add",
        {"a": 10, "b": 20},
        tool_context,
    )

    assert result.success is True
    assert result.data["result"] == 30
    assert result.error is None


@pytest.mark.asyncio
async def test_tool_plugin_execute_failure(tool_plugin: ConcreteToolPlugin, tool_context: ToolContext):
    """Test tool execution failure."""
    result = await tool_plugin.execute(
        "error_tool",
        {},
        tool_context,
    )

    assert result.success is False
    assert result.error == "Simulated error"
    assert result.data is None


@pytest.mark.asyncio
async def test_tool_plugin_unknown_tool(tool_plugin: ConcreteToolPlugin, tool_context: ToolContext):
    """Test execution of unknown tool."""
    result = await tool_plugin.execute(
        "nonexistent_tool",
        {},
        tool_context,
    )

    assert result.success is False
    assert "Unknown tool" in result.error


@pytest.mark.asyncio
async def test_tool_plugin_records_executions(tool_plugin: ConcreteToolPlugin, tool_context: ToolContext):
    """Test that plugin records all executions."""
    await tool_plugin.execute("add", {"a": 1, "b": 2}, tool_context)
    await tool_plugin.execute("add", {"a": 3, "b": 4}, tool_context)

    executions = tool_plugin.get_executions()
    assert len(executions) == 2
    assert executions[0][0] == "add"
    assert executions[0][1] == {"a": 1, "b": 2}
    assert executions[1][0] == "add"
    assert executions[1][1] == {"a": 3, "b": 4}


def test_tool_plugin_has_tools(tool_plugin: ConcreteToolPlugin, tool_definitions: list[ToolDefinition]):
    """Test that plugin exposes tool definitions."""
    assert len(tool_plugin.tools) == 2
    assert tool_plugin.tools[0].name == "add"
    assert tool_plugin.tools[1].name == "error_tool"
