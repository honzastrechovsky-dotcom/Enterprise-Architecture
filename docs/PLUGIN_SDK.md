# Plugin SDK Documentation

## Overview

The Plugin SDK allows developers to extend the Enterprise Agent Platform with custom capabilities. Plugins can add new tools, intercept agent lifecycle hooks, and integrate external services in a tenant-scoped manner.

## Architecture

### Core Components

1. **BasePlugin**: Abstract base class for all plugins
2. **PluginMetadata**: Plugin identification and requirements
3. **PluginHook**: Lifecycle hook points
4. **PluginRegistry**: Central plugin discovery and management
5. **PluginLoader**: Safe plugin loading with sandbox restrictions
6. **BaseToolPlugin**: Specialized plugin type for custom tools

### Plugin Lifecycle

```
Load → Initialize → Execute → Unload
  ↓        ↓           ↓        ↓
on_load  validate   handle_hook  on_unload
```

## Creating a Plugin

### Basic Plugin Structure

```python
from src.plugins.base import BasePlugin, PluginContext, PluginHook, PluginMetadata

class MyPlugin(BasePlugin):
    def __init__(self):
        self._metadata = PluginMetadata(
            name="my_plugin",
            version="1.0.0",
            author="Your Name",
            description="A custom plugin",
            required_permissions=["custom:action"],
            compatible_versions=["0.1.0"],
        )

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    async def on_load(self, context: PluginContext) -> None:
        # Initialize resources
        pass

    async def on_unload(self) -> None:
        # Clean up resources
        pass

    async def handle_hook(self, hook: PluginHook, data: dict) -> dict:
        # Handle lifecycle hooks
        return data
```

### Tool Plugin

For plugins that add custom tools:

```python
from src.plugins.tool_plugin import BaseToolPlugin, ToolDefinition, ToolResult, ToolContext

class CalculatorPlugin(BaseToolPlugin):
    def __init__(self):
        self._metadata = PluginMetadata(
            name="calculator",
            version="1.0.0",
            author="Your Name",
            description="Arithmetic calculator",
        )

        self._tools = [
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
        ]

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    @property
    def tools(self) -> list[ToolDefinition]:
        return self._tools

    async def on_load(self, context: PluginContext) -> None:
        pass

    async def on_unload(self) -> None:
        pass

    async def execute(self, tool_name: str, params: dict, context: ToolContext) -> ToolResult:
        if tool_name == "add":
            result = params["a"] + params["b"]
            return ToolResult(success=True, data={"result": result})

        return ToolResult(success=False, error=f"Unknown tool: {tool_name}")
```

## Plugin Hooks

Plugins can intercept agent lifecycle events:

- **BEFORE_AGENT_RUN**: Before agent starts processing
- **AFTER_AGENT_RUN**: After agent completes processing
- **BEFORE_TOOL_CALL**: Before any tool is invoked
- **AFTER_TOOL_CALL**: After tool execution completes
- **ON_ERROR**: When an error occurs
- **ON_REASONING_STEP**: During agent reasoning

### Hook Example

```python
async def handle_hook(self, hook: PluginHook, data: dict) -> dict:
    if hook == PluginHook.BEFORE_TOOL_CALL:
        # Log tool invocation
        log.info("tool_call", tool_name=data.get("tool_name"))

    elif hook == PluginHook.ON_ERROR:
        # Handle error
        error = data.get("error")
        # Send to monitoring service...

    return data
```

## Security & Sandboxing

### Forbidden Imports

Plugins cannot import these modules:

- `os`, `subprocess`, `sys`
- `socket`, `urllib`, `requests`, `httpx`
- `multiprocessing`, `threading`
- `eval`, `exec`, `compile`

### Allowed Imports

Safe modules:

- `json`, `datetime`, `uuid`, `typing`, `dataclasses`
- `re`, `math`, `decimal`, `collections`, `itertools`
- `src.plugins.*` (plugin SDK)

### Validation

```python
from src.plugins.loader import PluginSandbox

sandbox = PluginSandbox()

code = """
import os  # Forbidden!
"""

errors = sandbox.validate_code(code)
# Returns: ["Forbidden import: os"]
```

## Registration & Management

### Programmatic Registration

```python
from src.plugins.registry import get_registry

registry = get_registry()

# Global registration (all tenants)
plugin = MyPlugin()
registry.register(plugin)

# Tenant-scoped registration
registry.register(plugin, tenant_id="tenant-uuid")
```

### API Endpoints

#### List Available Plugins

```http
GET /api/v1/plugins
Authorization: Bearer <token>
```

Response:

```json
{
  "available": [
    {
      "name": "calculator",
      "version": "1.0.0",
      "author": "Enterprise Agent Platform",
      "description": "Arithmetic calculator",
      "required_permissions": ["math:calculate"],
      "compatible_versions": ["0.1.0"]
    }
  ],
  "registered": [
    {
      "id": "uuid",
      "plugin_name": "calculator",
      "plugin_version": "1.0.0",
      "enabled": true,
      "config": {},
      "installed_at": "2026-02-17T00:00:00Z",
      "updated_at": "2026-02-17T00:00:00Z"
    }
  ]
}
```

#### Enable Plugin

```http
POST /api/v1/plugins/{plugin_name}/enable
Authorization: Bearer <token>
```

#### Disable Plugin

```http
POST /api/v1/plugins/{plugin_name}/disable
Authorization: Bearer <token>
```

#### Get Plugin Configuration

```http
GET /api/v1/plugins/{plugin_name}/config
Authorization: Bearer <token>
```

#### Update Plugin Configuration

```http
PUT /api/v1/plugins/{plugin_name}/config
Authorization: Bearer <token>
Content-Type: application/json

{
  "config": {
    "setting1": "value1",
    "setting2": 42
  }
}
```

## Loading Plugins

### From Module Path

```python
from src.plugins.loader import load_plugin_from_module

plugin = load_plugin_from_module("plugins.calculator")
```

### From Directory

```python
from src.plugins.loader import load_plugin_from_directory

plugins = load_plugin_from_directory("/path/to/plugins")
```

## Database Schema

### plugin_registrations Table

| Column          | Type      | Description                      |
| --------------- | --------- | -------------------------------- |
| id              | UUID      | Primary key                      |
| tenant_id       | UUID      | Foreign key to tenants           |
| plugin_name     | String    | Plugin identifier                |
| plugin_version  | String    | Plugin version (semver)          |
| enabled         | Boolean   | Whether plugin is active         |
| config          | JSONB     | Plugin-specific configuration    |
| installed_at    | Timestamp | Installation time                |
| updated_at      | Timestamp | Last update time                 |

**Indexes:**

- `uq_plugin_tenant`: Unique constraint on (tenant_id, plugin_name)
- `ix_plugin_enabled`: Index on (tenant_id, enabled)

## Best Practices

### 1. Idempotent Operations

Ensure `on_load` and `on_unload` can be called multiple times safely:

```python
async def on_load(self, context: PluginContext) -> None:
    if self._initialized:
        return
    # Initialize...
    self._initialized = True
```

### 2. Error Handling

Always return ToolResult with clear error messages:

```python
try:
    result = perform_operation(params)
    return ToolResult(success=True, data={"result": result})
except ValueError as e:
    return ToolResult(success=False, error=f"Invalid parameter: {e}")
```

### 3. Configuration Validation

Validate configuration in `on_load`:

```python
async def on_load(self, context: PluginContext) -> None:
    config = context.settings
    if "api_key" not in config:
        raise ValueError("Missing required configuration: api_key")
```

### 4. Logging

Use structured logging:

```python
import structlog

log = structlog.get_logger(__name__)

log.info("plugin.operation", operation="add", result=42)
```

### 5. Timeouts

Implement timeouts for external calls:

```python
async def execute(self, tool_name: str, params: dict, context: ToolContext) -> ToolResult:
    try:
        async with asyncio.timeout(30):  # 30 second timeout
            result = await external_api_call(params)
        return ToolResult(success=True, data=result)
    except asyncio.TimeoutError:
        return ToolResult(success=False, error="Operation timed out")
```

## Testing

### Unit Tests

```python
import pytest
from src.plugins.examples.calculator import CalculatorPlugin

@pytest.fixture
def plugin():
    return CalculatorPlugin()

@pytest.mark.asyncio
async def test_calculator_add(plugin):
    result = await plugin.execute(
        "add",
        {"a": 2, "b": 3},
        ToolContext(tenant_id=uuid.uuid4()),
    )
    assert result.success is True
    assert result.data["result"] == 5
```

### Integration Tests

Mark integration tests that require database:

```python
@pytest.mark.integration
async def test_plugin_registration(db, plugin):
    registry = get_registry()
    registry.register(plugin)

    retrieved = registry.get_plugin(plugin.metadata.name)
    assert retrieved is not None
```

## Migration Guide

### From Agent to Plugin

If you have custom agent code, migrate to a plugin:

**Before (Agent Code):**

```python
def calculate(a, b):
    return a + b
```

**After (Plugin):**

```python
class CalculatorPlugin(BaseToolPlugin):
    async def execute(self, tool_name: str, params: dict, context: ToolContext):
        if tool_name == "add":
            return ToolResult(
                success=True,
                data={"result": params["a"] + params["b"]}
            )
```

## Troubleshooting

### Plugin Not Loading

1. Check plugin name uniqueness
2. Verify metadata is valid
3. Ensure no forbidden imports
4. Check logs for validation errors

### Plugin Not Appearing in API

1. Verify plugin is registered
2. Check tenant scope (global vs. tenant-specific)
3. Confirm user has admin role

### Tool Execution Fails

1. Validate parameters match JSON Schema
2. Check error logs for exceptions
3. Verify ToolContext has required fields
4. Test tool in isolation with unit tests

## Example: Complete Plugin

See `src/plugins/examples/calculator.py` for a complete, working example.

## Support

For questions or issues:

1. Check existing plugins in `src/plugins/examples/`
2. Review test cases in `tests/plugins/`
3. Consult API documentation at `/docs` (Swagger UI)
