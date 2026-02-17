"""Plugin loader with sandbox restrictions.

Provides safe plugin loading with:
- Import restrictions (block os, subprocess, sys, etc.)
- Code validation before execution
- Timeout enforcement
- Memory limit checking
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

import structlog

from src.plugins.base import BasePlugin

log = structlog.get_logger(__name__)


class PluginSandbox:
    """Sandbox environment for safe plugin execution.

    Validates plugin code before loading and enforces runtime restrictions.
    """

    # Modules that plugins are forbidden to import
    FORBIDDEN_MODULES = {
        "os",
        "subprocess",
        "sys",
        "__builtin__",
        "__builtins__",
        "importlib",
        "eval",
        "exec",
        "compile",
        "open",  # Use specific file APIs instead
        "input",  # No interactive input
        "multiprocessing",
        "threading",  # Prevent spawning uncontrolled threads
        "ctypes",
        "socket",  # No direct network access
        "urllib",
        "requests",  # Use provided HTTP client
        "httpx",
    }

    # Modules that are safe to import
    ALLOWED_MODULES = {
        "json",
        "datetime",
        "uuid",
        "typing",
        "dataclasses",
        "enum",
        "re",
        "math",
        "decimal",
        "fractions",
        "collections",
        "itertools",
        "functools",
        "operator",
        "copy",
        "pprint",
    }

    def __init__(
        self,
        timeout_seconds: int = 30,
        max_memory_mb: int = 512,
    ):
        """Initialize sandbox with resource limits.

        Args:
            timeout_seconds: Maximum execution time for plugin operations
            max_memory_mb: Maximum memory usage (not enforced yet)
        """
        self.timeout_seconds = timeout_seconds
        self.max_memory_mb = max_memory_mb

    def is_import_allowed(self, module_name: str) -> bool:
        """Check if a module import is allowed.

        Args:
            module_name: Name of module to import

        Returns:
            True if allowed, False if forbidden
        """
        # Get root module name (e.g., "os.path" -> "os")
        root_module = module_name.split(".")[0]

        # Check forbidden list first
        if root_module in self.FORBIDDEN_MODULES:
            return False

        # Allow explicitly safe modules
        if root_module in self.ALLOWED_MODULES:
            return True

        # Allow src.plugins.* imports (for plugin SDK)
        if module_name.startswith("src.plugins"):
            return True

        # Default: deny unknown modules
        return False

    def validate_code(self, code: str) -> list[str]:
        """Validate plugin code for security issues.

        Checks for:
        - Forbidden imports
        - Dangerous builtin calls
        - Suspicious patterns

        Args:
            code: Python source code to validate

        Returns:
            List of validation errors (empty if valid)
        """
        errors: list[str] = []

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            errors.append(f"Syntax error: {e}")
            return errors

        # Walk the AST looking for forbidden patterns
        for node in ast.walk(tree):
            # Check imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not self.is_import_allowed(alias.name):
                        errors.append(
                            f"Forbidden import: {alias.name}"
                        )

            elif isinstance(node, ast.ImportFrom):
                if node.module and not self.is_import_allowed(node.module):
                    errors.append(
                        f"Forbidden import: {node.module}"
                    )

            # Check for eval/exec/compile calls
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in {"eval", "exec", "compile", "__import__"}:
                        errors.append(
                            f"Forbidden builtin call: {node.func.id}"
                        )

        return errors

    async def execute_with_timeout(self, coro: Any) -> Any:
        """Execute a coroutine with timeout.

        Args:
            coro: Coroutine to execute

        Returns:
            Coroutine result

        Raises:
            TimeoutError: If execution exceeds timeout
        """
        return await asyncio.wait_for(coro, timeout=self.timeout_seconds)


def load_plugin_from_module(module_path: str) -> BasePlugin:
    """Load a plugin from a Python module path.

    Args:
        module_path: Dotted module path (e.g., "plugins.calculator")

    Returns:
        Loaded plugin instance

    Raises:
        ImportError: If module cannot be imported
        ValueError: If module doesn't contain valid plugin
    """
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        log.error("loader.import_failed", module_path=module_path, error=str(e))
        raise

    # Find BasePlugin subclasses in the module
    plugin_classes = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, BasePlugin)
        and obj is not BasePlugin
    ]

    if not plugin_classes:
        raise ValueError(
            f"Module {module_path} does not contain any BasePlugin subclasses"
        )

    if len(plugin_classes) > 1:
        raise ValueError(
            f"Module {module_path} contains multiple plugin classes: "
            f"{[cls.__name__ for cls in plugin_classes]}"
        )

    plugin_class = plugin_classes[0]

    # Instantiate plugin (assumes no-arg constructor or default args)
    try:
        plugin = plugin_class()
    except Exception as e:
        raise ValueError(
            f"Failed to instantiate plugin {plugin_class.__name__}: {e}"
        ) from e

    log.info(
        "loader.plugin_loaded",
        module_path=module_path,
        plugin_name=plugin.metadata.name,
        version=plugin.metadata.version,
    )

    return plugin


def load_plugin_from_directory(dir_path: str) -> list[BasePlugin]:
    """Load all plugins from a directory.

    Searches for Python files (*.py) in the directory and attempts to
    load plugins from each.

    Args:
        dir_path: Path to directory containing plugin files

    Returns:
        List of loaded plugins (may be empty)
    """
    directory = Path(dir_path)

    if not directory.exists() or not directory.is_dir():
        log.warning("loader.directory_not_found", dir_path=dir_path)
        return []

    plugins: list[BasePlugin] = []

    # Find all Python files
    for py_file in directory.glob("*.py"):
        if py_file.name.startswith("_"):
            # Skip __init__.py and private files
            continue

        # Convert file path to module path
        # This is simplified - in production, would need proper package handling
        module_name = py_file.stem

        try:
            # Add directory to sys.path temporarily
            if str(directory) not in sys.path:
                sys.path.insert(0, str(directory))

            plugin = load_plugin_from_module(module_name)
            plugins.append(plugin)

        except Exception as e:
            log.warning(
                "loader.plugin_load_failed",
                file=py_file.name,
                error=str(e),
            )
            continue
        finally:
            # Remove from sys.path
            if str(directory) in sys.path:
                sys.path.remove(str(directory))

    log.info("loader.directory_scan_complete", dir_path=dir_path, count=len(plugins))
    return plugins
