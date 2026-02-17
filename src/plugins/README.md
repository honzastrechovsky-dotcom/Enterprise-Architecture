# Plugin System

## Quick Start

### Creating a Plugin

```python
from src.plugins.base import BasePlugin, PluginContext, PluginMetadata

class MyPlugin(BasePlugin):
    def __init__(self):
        self._metadata = PluginMetadata(
            name="my_plugin",
            version="1.0.0",
            author="Your Name",
            description="My custom plugin",
        )

    @property
    def metadata(self) -> PluginMetadata:
        return self._metadata

    async def on_load(self, context: PluginContext) -> None:
        # Initialize plugin
        pass

    async def on_unload(self) -> None:
        # Cleanup
        pass

    async def handle_hook(self, hook: PluginHook, data: dict) -> dict:
        # Handle lifecycle hooks
        return data
```

### Creating a Tool Plugin

```python
from src.plugins.tool_plugin import BaseToolPlugin, ToolDefinition, ToolResult, ToolContext

class MyToolPlugin(BaseToolPlugin):
    @property
    def tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="my_tool",
                description="Does something useful",
                parameters={
                    "type": "object",
                    "properties": {
                        "input": {"type": "string"}
                    },
                    "required": ["input"]
                }
            )
        ]

    async def execute(self, tool_name: str, params: dict, context: ToolContext) -> ToolResult:
        if tool_name == "my_tool":
            return ToolResult(success=True, data={"output": "processed"})
        return ToolResult(success=False, error="Unknown tool")
```

### Registering a Plugin

```python
from src.plugins.registry import get_registry

registry = get_registry()

# Global registration
plugin = MyPlugin()
registry.register(plugin)

# Tenant-scoped registration
registry.register(plugin, tenant_id="tenant-uuid")
```

## Directory Structure

```
src/plugins/
├── __init__.py           # Package exports
├── base.py               # BasePlugin, PluginMetadata, PluginHook
├── tool_plugin.py        # BaseToolPlugin, ToolDefinition
├── registry.py           # PluginRegistry (singleton)
├── loader.py             # PluginLoader, PluginSandbox
└── examples/
    ├── __init__.py
    └── calculator.py     # Example calculator plugin
```

## Key Concepts

### Plugin Metadata

Every plugin must declare:

- **name**: Unique identifier
- **version**: Semver version string
- **author**: Plugin author
- **description**: What the plugin does
- **required_permissions**: Permissions needed (optional)
- **compatible_versions**: Platform versions supported (optional)

### Plugin Lifecycle

1. **Load**: `on_load(context)` - Initialize resources
2. **Execute**: `handle_hook(hook, data)` or `execute(tool_name, params, context)`
3. **Unload**: `on_unload()` - Cleanup resources

### Plugin Hooks

Lifecycle interception points:

- `BEFORE_AGENT_RUN` - Before agent processing
- `AFTER_AGENT_RUN` - After agent completion
- `BEFORE_TOOL_CALL` - Before tool execution
- `AFTER_TOOL_CALL` - After tool execution
- `ON_ERROR` - Error occurred
- `ON_REASONING_STEP` - During reasoning

### Security Sandbox

Plugins are validated before loading:

- Forbidden imports blocked (os, subprocess, sys, socket, etc.)
- Code validation via AST inspection
- Execution timeouts enforced
- Memory limits (configurable)

## API Endpoints

All endpoints require admin role:

- `GET /api/v1/plugins` - List plugins
- `POST /api/v1/plugins/{name}/enable` - Enable plugin
- `POST /api/v1/plugins/{name}/disable` - Disable plugin
- `GET /api/v1/plugins/{name}/config` - Get configuration
- `PUT /api/v1/plugins/{name}/config` - Update configuration

## Database Schema

### plugin_registrations

Tracks which plugins are enabled per tenant:

```sql
CREATE TABLE plugin_registrations (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    plugin_name VARCHAR(255) NOT NULL,
    plugin_version VARCHAR(64) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    config JSONB NOT NULL DEFAULT '{}',
    installed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, plugin_name)
);
```

## Testing

Run plugin tests:

```bash
pytest tests/plugins/ -v
```

Test categories:

- `test_plugin_base.py` - Base plugin contracts
- `test_tool_plugin.py` - Tool plugin contracts
- `test_registry.py` - Registry operations
- `test_loader.py` - Plugin loading & sandboxing
- `test_calculator_example.py` - Example plugin

## Examples

See `src/plugins/examples/calculator.py` for a complete, working tool plugin.

## Documentation

Full documentation: `/docs/PLUGIN_SDK.md`

## Architecture

```
┌─────────────────┐
│  FastAPI Routes │
│  (Admin API)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐      ┌──────────────┐
│ PluginRegistry  │◄─────┤ PluginLoader │
│   (Singleton)   │      │  (Sandbox)   │
└────────┬────────┘      └──────────────┘
         │
         ▼
┌─────────────────┐
│   BasePlugin    │
│     (ABC)       │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌────────┐  ┌──────────────┐
│Plugin1 │  │BaseToolPlugin│
└────────┘  └──────┬───────┘
                   │
                   ▼
            ┌──────────────┐
            │ Calculator   │
            │   Plugin     │
            └──────────────┘
```

## Migration

Database migration: `alembic/versions/004_add_plugins.py`

Run migration:

```bash
alembic upgrade head
```

## Support

For issues or questions:

1. Check example plugins
2. Review test cases
3. Read full SDK documentation
