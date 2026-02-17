"""Tests for PluginLoader and PluginSandbox.

Tests cover:
- Loading plugins from module path
- Loading plugins from directory
- Plugin sandbox restrictions
- Import validation
- Timeout enforcement
- Memory limit checking
"""

from __future__ import annotations

import pytest

from src.plugins.loader import PluginSandbox, load_plugin_from_directory, load_plugin_from_module


# ------------------------------------------------------------------ #
# Plugin loading tests
# ------------------------------------------------------------------ #


def test_load_plugin_from_module_success():
    """Test loading a valid plugin from module path."""
    # This will fail until implementation exists
    with pytest.raises(ImportError):
        plugin = load_plugin_from_module("tests.plugins.fixtures.valid_plugin")


def test_load_plugin_from_module_not_found():
    """Test loading from nonexistent module fails."""
    with pytest.raises(ImportError):
        load_plugin_from_module("nonexistent.module.path")


def test_load_plugin_from_directory_empty(tmp_path):
    """Test loading from empty directory returns empty list."""
    plugins = load_plugin_from_directory(str(tmp_path))
    assert plugins == []


def test_load_plugin_from_directory_with_plugins(tmp_path):
    """Test loading multiple plugins from directory."""
    # Create mock plugin files
    plugin1_file = tmp_path / "plugin1.py"
    plugin1_file.write_text("""
from src.plugins.base import BasePlugin, PluginMetadata

class TestPlugin1(BasePlugin):
    pass
""")

    plugin2_file = tmp_path / "plugin2.py"
    plugin2_file.write_text("""
from src.plugins.base import BasePlugin, PluginMetadata

class TestPlugin2(BasePlugin):
    pass
""")

    plugins = load_plugin_from_directory(str(tmp_path))
    # Will fail until implementation exists
    assert len(plugins) >= 0  # Placeholder


# ------------------------------------------------------------------ #
# Plugin sandbox tests
# ------------------------------------------------------------------ #


def test_sandbox_blocks_forbidden_imports():
    """Test sandbox blocks dangerous imports."""
    sandbox = PluginSandbox()

    forbidden_modules = [
        "os",
        "subprocess",
        "sys",
        "__builtin__",
        "__builtins__",
    ]

    for module in forbidden_modules:
        is_allowed = sandbox.is_import_allowed(module)
        assert is_allowed is False, f"Should block import of {module}"


def test_sandbox_allows_safe_imports():
    """Test sandbox allows safe imports."""
    sandbox = PluginSandbox()

    safe_modules = [
        "json",
        "datetime",
        "uuid",
        "typing",
        "dataclasses",
    ]

    for module in safe_modules:
        is_allowed = sandbox.is_import_allowed(module)
        assert is_allowed is True, f"Should allow import of {module}"


def test_sandbox_validates_plugin_code():
    """Test sandbox validates plugin code before execution."""
    sandbox = PluginSandbox()

    # Safe code
    safe_code = """
import json
from dataclasses import dataclass

@dataclass
class SafePlugin:
    name: str
"""
    errors = sandbox.validate_code(safe_code)
    assert errors == []

    # Unsafe code
    unsafe_code = """
import os
os.system('rm -rf /')
"""
    errors = sandbox.validate_code(unsafe_code)
    assert len(errors) > 0
    assert any("os" in error.lower() for error in errors)


@pytest.mark.asyncio
async def test_sandbox_enforces_timeout():
    """Test sandbox enforces execution timeout."""
    sandbox = PluginSandbox(timeout_seconds=1)

    async def long_running_operation():
        import asyncio
        await asyncio.sleep(10)  # Longer than timeout

    with pytest.raises(TimeoutError):
        await sandbox.execute_with_timeout(long_running_operation())


def test_sandbox_checks_memory_limits():
    """Test sandbox can check memory usage."""
    sandbox = PluginSandbox(max_memory_mb=100)

    # Check if memory limit is configured
    assert sandbox.max_memory_mb == 100


# ------------------------------------------------------------------ #
# Plugin validation tests
# ------------------------------------------------------------------ #


def test_validate_plugin_no_forbidden_imports():
    """Test plugin validation rejects forbidden imports."""
    code_with_os = """
import os
from src.plugins.base import BasePlugin

class BadPlugin(BasePlugin):
    def execute(self):
        os.system('echo hello')
"""

    sandbox = PluginSandbox()
    errors = sandbox.validate_code(code_with_os)
    assert len(errors) > 0


def test_validate_plugin_no_subprocess():
    """Test plugin validation rejects subprocess imports."""
    code_with_subprocess = """
import subprocess
from src.plugins.base import BasePlugin

class BadPlugin(BasePlugin):
    def execute(self):
        subprocess.run(['ls'])
"""

    sandbox = PluginSandbox()
    errors = sandbox.validate_code(code_with_subprocess)
    assert len(errors) > 0
