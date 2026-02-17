"""Tests for the example calculator plugin.

Tests cover:
- Plugin initialization
- Tool definitions
- Tool execution (add, subtract, multiply, divide)
- Error handling (division by zero, missing parameters)
- Plugin lifecycle
"""

from __future__ import annotations

import uuid

import pytest

from src.plugins.examples.calculator import CalculatorPlugin
from src.plugins.base import PluginContext
from src.plugins.tool_plugin import ToolContext


@pytest.fixture
def calculator_plugin():
    """Create calculator plugin instance."""
    return CalculatorPlugin()


@pytest.fixture
def plugin_context():
    """Create plugin context."""
    return PluginContext(
        tenant_id=uuid.uuid4(),
        settings={},
        logger=None,
    )


@pytest.fixture
def tool_context():
    """Create tool context."""
    return ToolContext(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
    )


# ------------------------------------------------------------------ #
# Plugin metadata tests
# ------------------------------------------------------------------ #


def test_calculator_plugin_metadata(calculator_plugin: CalculatorPlugin):
    """Test calculator plugin has correct metadata."""
    metadata = calculator_plugin.metadata

    assert metadata.name == "calculator"
    assert metadata.version == "1.0.0"
    assert metadata.author == "Enterprise Agent Platform"
    assert "arithmetic" in metadata.description.lower()
    assert "math:calculate" in metadata.required_permissions


def test_calculator_plugin_tools(calculator_plugin: CalculatorPlugin):
    """Test calculator plugin provides expected tools."""
    tools = calculator_plugin.tools

    assert len(tools) == 4
    tool_names = {tool.name for tool in tools}
    assert tool_names == {"add", "subtract", "multiply", "divide"}

    # Check each tool has required fields
    for tool in tools:
        assert tool.name
        assert tool.description
        assert "properties" in tool.parameters
        assert "a" in tool.parameters["properties"]
        assert "b" in tool.parameters["properties"]


# ------------------------------------------------------------------ #
# Tool execution tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_calculator_add(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test addition operation."""
    result = await calculator_plugin.execute(
        "add",
        {"a": 10, "b": 5},
        tool_context,
    )

    assert result.success is True
    assert result.data["result"] == 15
    assert result.error is None
    assert result.metadata["operation"] == "add"


@pytest.mark.asyncio
async def test_calculator_subtract(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test subtraction operation."""
    result = await calculator_plugin.execute(
        "subtract",
        {"a": 20, "b": 8},
        tool_context,
    )

    assert result.success is True
    assert result.data["result"] == 12
    assert result.metadata["operation"] == "subtract"


@pytest.mark.asyncio
async def test_calculator_multiply(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test multiplication operation."""
    result = await calculator_plugin.execute(
        "multiply",
        {"a": 6, "b": 7},
        tool_context,
    )

    assert result.success is True
    assert result.data["result"] == 42
    assert result.metadata["operation"] == "multiply"


@pytest.mark.asyncio
async def test_calculator_divide(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test division operation."""
    result = await calculator_plugin.execute(
        "divide",
        {"a": 20, "b": 4},
        tool_context,
    )

    assert result.success is True
    assert result.data["result"] == 5.0
    assert result.metadata["operation"] == "divide"


# ------------------------------------------------------------------ #
# Error handling tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_calculator_divide_by_zero(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test division by zero error handling."""
    result = await calculator_plugin.execute(
        "divide",
        {"a": 10, "b": 0},
        tool_context,
    )

    assert result.success is False
    assert result.error is not None
    assert "zero" in result.error.lower()
    assert result.data is None


@pytest.mark.asyncio
async def test_calculator_missing_parameters(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test missing parameter error handling."""
    result = await calculator_plugin.execute(
        "add",
        {"a": 10},  # Missing 'b'
        tool_context,
    )

    assert result.success is False
    assert result.error is not None
    assert "missing" in result.error.lower() or "required" in result.error.lower()


@pytest.mark.asyncio
async def test_calculator_unknown_tool(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test unknown tool error handling."""
    result = await calculator_plugin.execute(
        "power",  # Not implemented
        {"a": 2, "b": 3},
        tool_context,
    )

    assert result.success is False
    assert result.error is not None
    assert "unknown" in result.error.lower()


# ------------------------------------------------------------------ #
# Plugin lifecycle tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_calculator_lifecycle(
    calculator_plugin: CalculatorPlugin,
    plugin_context: PluginContext,
):
    """Test calculator plugin lifecycle."""
    # on_load should not raise
    await calculator_plugin.on_load(plugin_context)

    # Plugin should be usable
    result = await calculator_plugin.execute(
        "add",
        {"a": 1, "b": 1},
        ToolContext(tenant_id=plugin_context.tenant_id),
    )
    assert result.success is True

    # on_unload should not raise
    await calculator_plugin.on_unload()


# ------------------------------------------------------------------ #
# Edge case tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_calculator_negative_numbers(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test operations with negative numbers."""
    result = await calculator_plugin.execute(
        "add",
        {"a": -5, "b": 3},
        tool_context,
    )

    assert result.success is True
    assert result.data["result"] == -2


@pytest.mark.asyncio
async def test_calculator_decimal_numbers(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test operations with decimal numbers."""
    result = await calculator_plugin.execute(
        "multiply",
        {"a": 2.5, "b": 4.0},
        tool_context,
    )

    assert result.success is True
    assert result.data["result"] == 10.0


@pytest.mark.asyncio
async def test_calculator_large_numbers(
    calculator_plugin: CalculatorPlugin,
    tool_context: ToolContext,
):
    """Test operations with large numbers."""
    result = await calculator_plugin.execute(
        "add",
        {"a": 1000000, "b": 2000000},
        tool_context,
    )

    assert result.success is True
    assert result.data["result"] == 3000000
