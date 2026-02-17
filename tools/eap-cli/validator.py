"""Plugin structure, manifest, security, and test validation.

Phase 11B: Plugin Dev Kit.

``PluginValidator`` performs four independent validation passes:

1. ``validate_structure`` - Required files exist with correct layout
2. ``validate_manifest``  - plugin.yaml parses and has required fields
3. ``validate_security``  - AST scan for dangerous imports/calls
4. ``validate_tests``     - Test directory exists with test files

All passes return a ``ValidationResult`` containing lists of errors and
warnings.  An empty ``errors`` list means the pass succeeded.

Usage::

    from tools.eap_cli.validator import PluginValidator

    validator = PluginValidator()
    result = validator.validate_structure("/path/to/my-plugin")
    if result.errors:
        for err in result.errors:
            print(f"ERROR: {err}")
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Use only stdlib - no external deps (per spec)
try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

try:
    import yaml as _yaml  # Optional YAML support

    def _load_yaml(text: str) -> Any:
        return _yaml.safe_load(text)

except ImportError:
    def _load_yaml(text: str) -> Any:  # type: ignore[misc]
        """Minimal YAML parser for simple key: value manifests (no external dep)."""
        result: dict[str, Any] = {}
        current_key: str | None = None
        current_list: list[str] | None = None

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line or line.startswith("#"):
                continue

            # Detect list items
            stripped = line.lstrip()
            if stripped.startswith("- ") and current_key and current_list is not None:
                current_list.append(stripped[2:].strip())
                result[current_key] = current_list
                continue

            if ":" in line:
                indent = len(line) - len(line.lstrip())
                if indent == 0:
                    current_list = None
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if not value:
                    # Could be a list or nested dict next
                    current_key = key
                    current_list = []
                    result[key] = current_list
                else:
                    current_key = key
                    result[key] = value

        return result


# ------------------------------------------------------------------ #
# Data classes
# ------------------------------------------------------------------ #

REQUIRED_FILES = frozenset(
    {
        "plugin.py",
        "plugin.yaml",
        "__init__.py",
    }
)

REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "name",
        "version",
        "description",
        "author",
        "min_platform_version",
    }
)

# Dangerous imports that should not appear in plugins
BLOCKED_MODULES = frozenset(
    {
        "subprocess",
        "os.system",
        "pty",
        "ctypes",
        "cffi",
        "socket",  # raw socket access
        "signal",
        "multiprocessing",
        "threading",  # warn only
        "importlib",
    }
)

# Dangerous function calls (as attribute chains)
BLOCKED_CALLS = frozenset(
    {
        "os.system",
        "os.popen",
        "os.execv",
        "os.execle",
        "os.execvp",
        "os.execvpe",
        "os.spawnl",
        "os.spawnv",
        "subprocess.run",
        "subprocess.call",
        "subprocess.Popen",
        "subprocess.check_output",
        "eval",
        "exec",
        "compile",
        "__import__",
    }
)

WARNING_MODULES = frozenset({"threading", "asyncio"})


@dataclass
class ValidationResult:
    """Result of a validation pass.

    Attributes:
        passed:   True if no errors were found.
        errors:   List of error messages (strings).
        warnings: List of warning messages (strings).
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return True if there are no errors."""
        return len(self.errors) == 0

    def merge(self, other: "ValidationResult") -> "ValidationResult":
        """Merge another result into this one (mutates self, returns self)."""
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        return self


# ------------------------------------------------------------------ #
# Validator
# ------------------------------------------------------------------ #


class PluginValidator:
    """Validate an EAP plugin directory against the Plugin Dev Kit spec."""

    # ---------------------------------------------------------------- #
    # Structure validation
    # ---------------------------------------------------------------- #

    def validate_structure(self, path: str | Path) -> ValidationResult:
        """Check that the required plugin files exist.

        Required files:
        - plugin.py      - Main plugin implementation
        - plugin.yaml    - Plugin manifest
        - __init__.py    - Package init
        - tests/         - Test directory (warning if missing)

        Args:
            path: Path to the plugin root directory.

        Returns:
            ValidationResult with errors for missing required files.
        """
        result = ValidationResult()
        plugin_dir = Path(path)

        if not plugin_dir.is_dir():
            result.errors.append(f"Plugin path does not exist or is not a directory: {path}")
            return result

        for filename in REQUIRED_FILES:
            if not (plugin_dir / filename).is_file():
                result.errors.append(f"Required file missing: {filename}")

        tests_dir = plugin_dir / "tests"
        if not tests_dir.is_dir():
            result.warnings.append(
                "No 'tests/' directory found. "
                "Add tests to ensure your plugin works correctly."
            )

        readme = plugin_dir / "README.md"
        if not readme.is_file():
            result.warnings.append(
                "No README.md found. "
                "Consider adding documentation for plugin users."
            )

        return result

    # ---------------------------------------------------------------- #
    # Manifest validation
    # ---------------------------------------------------------------- #

    def validate_manifest(self, path: str | Path) -> ValidationResult:
        """Parse and validate plugin.yaml.

        Checks:
        - File is parseable YAML
        - All required fields are present
        - Version follows semver (x.y.z)
        - min_platform_version is a valid semver string

        Args:
            path: Path to the plugin root directory.

        Returns:
            ValidationResult with errors for manifest violations.
        """
        result = ValidationResult()
        manifest_path = Path(path) / "plugin.yaml"

        if not manifest_path.is_file():
            result.errors.append("plugin.yaml not found")
            return result

        try:
            text = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            result.errors.append(f"Cannot read plugin.yaml: {exc}")
            return result

        try:
            manifest: dict[str, Any] = _load_yaml(text) or {}
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"plugin.yaml is not valid YAML: {exc}")
            return result

        if not isinstance(manifest, dict):
            result.errors.append("plugin.yaml must be a YAML mapping at the top level")
            return result

        # Required fields
        for field_name in REQUIRED_MANIFEST_FIELDS:
            if field_name not in manifest or not manifest[field_name]:
                result.errors.append(f"plugin.yaml missing required field: '{field_name}'")

        # Semver checks
        version = manifest.get("version", "")
        if version and not _is_semver(str(version)):
            result.errors.append(
                f"plugin.yaml 'version' must follow semver (x.y.z): got '{version}'"
            )

        min_version = manifest.get("min_platform_version", "")
        if min_version and not _is_semver(str(min_version)):
            result.errors.append(
                f"plugin.yaml 'min_platform_version' must follow semver (x.y.z): "
                f"got '{min_version}'"
            )

        # Optional but recommended fields
        if "required_permissions" not in manifest:
            result.warnings.append(
                "plugin.yaml missing 'required_permissions'. "
                "Declare required permissions explicitly."
            )

        if "tool_definitions" not in manifest:
            result.warnings.append(
                "plugin.yaml missing 'tool_definitions'. "
                "Declare tool definitions for auto-discovery."
            )

        return result

    # ---------------------------------------------------------------- #
    # Security validation
    # ---------------------------------------------------------------- #

    def validate_security(self, path: str | Path) -> ValidationResult:
        """Perform an AST-based security scan on all Python source files.

        Checks for:
        - Blocked import statements (subprocess, os.system, etc.)
        - Dangerous function calls (eval, exec, etc.)
        - Use of __builtins__ dunder attributes for sandbox escapes

        Args:
            path: Path to the plugin root directory.

        Returns:
            ValidationResult with errors for security violations.
        """
        result = ValidationResult()
        plugin_dir = Path(path)

        if not plugin_dir.is_dir():
            result.errors.append(f"Plugin path does not exist: {path}")
            return result

        py_files = list(plugin_dir.rglob("*.py"))
        if not py_files:
            result.warnings.append("No Python source files found in plugin directory.")
            return result

        for py_file in py_files:
            # Skip test files (allow subprocess in tests for CI purposes)
            if "test" in py_file.name.lower():
                continue
            file_result = self._scan_file(py_file)
            result.merge(file_result)

        return result

    def _scan_file(self, py_file: Path) -> ValidationResult:
        """Scan a single Python file for security violations."""
        result = ValidationResult()

        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError as exc:
            result.warnings.append(f"Cannot read {py_file.name}: {exc}")
            return result

        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            result.errors.append(f"Syntax error in {py_file.name}: {exc}")
            return result

        for node in ast.walk(tree):
            # Check import statements
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._check_import(node, py_file, result)

            # Check function calls
            elif isinstance(node, ast.Call):
                self._check_call(node, py_file, result)

            # Check attribute access for dunder escape patterns
            elif isinstance(node, ast.Attribute):
                if node.attr.startswith("__") and node.attr.endswith("__"):
                    if node.attr in ("__builtins__", "__import__", "__loader__"):
                        result.errors.append(
                            f"{py_file.name}:{node.col_offset}: "
                            f"Dangerous dunder access: {node.attr!r}"
                        )

        return result

    def _check_import(
        self,
        node: ast.Import | ast.ImportFrom,
        py_file: Path,
        result: ValidationResult,
    ) -> None:
        """Check an import node for blocked modules."""
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if module in BLOCKED_MODULES:
                    result.errors.append(
                        f"{py_file.name}:{node.lineno}: "
                        f"Blocked import: '{module}'"
                    )
                elif module in WARNING_MODULES:
                    result.warnings.append(
                        f"{py_file.name}:{node.lineno}: "
                        f"Use of '{module}' detected. Review thread safety."
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in BLOCKED_MODULES or module.split(".")[0] in BLOCKED_MODULES:
                result.errors.append(
                    f"{py_file.name}:{node.lineno}: "
                    f"Blocked import from: '{module}'"
                )

    def _check_call(
        self,
        node: ast.Call,
        py_file: Path,
        result: ValidationResult,
    ) -> None:
        """Check a call node for blocked function calls."""
        call_str = _ast_call_to_str(node.func)
        if call_str in BLOCKED_CALLS:
            result.errors.append(
                f"{py_file.name}:{node.lineno}: "
                f"Blocked call: {call_str}()"
            )

    # ---------------------------------------------------------------- #
    # Test validation
    # ---------------------------------------------------------------- #

    def validate_tests(self, path: str | Path) -> ValidationResult:
        """Check that tests exist and pass.

        Checks:
        - tests/ directory exists
        - At least one test_*.py file is present
        - Tests pass (runs pytest, fails gracefully if pytest not found)

        Args:
            path: Path to the plugin root directory.

        Returns:
            ValidationResult with errors if tests are missing or fail.
        """
        result = ValidationResult()
        plugin_dir = Path(path)
        tests_dir = plugin_dir / "tests"

        if not tests_dir.is_dir():
            result.errors.append(
                "No 'tests/' directory found. "
                "Plugin must include tests in a 'tests/' directory."
            )
            return result

        test_files = list(tests_dir.glob("test_*.py"))
        if not test_files:
            result.errors.append(
                "No test files (test_*.py) found in 'tests/' directory."
            )
            return result

        result.warnings.append(
            f"Found {len(test_files)} test file(s). "
            "Run 'eap plugin test <path>' to execute them."
        )

        # Attempt to run tests if pytest is available
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", str(tests_dir), "-q", "--tb=short"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                result.errors.append(
                    f"Plugin tests failed:\n{proc.stdout}\n{proc.stderr}"
                )
            else:
                result.warnings = [
                    w for w in result.warnings
                    if "Run 'eap plugin test'" not in w
                ]
        except FileNotFoundError:
            result.warnings.append(
                "pytest not found - skipping test execution. "
                "Install pytest to enable automated test validation."
            )
        except subprocess.TimeoutExpired:
            result.errors.append("Plugin tests timed out after 60 seconds.")

        return result

    # ---------------------------------------------------------------- #
    # Combined validation
    # ---------------------------------------------------------------- #

    def validate_all(self, path: str | Path) -> ValidationResult:
        """Run all validation passes and return combined results.

        Args:
            path: Path to the plugin root directory.

        Returns:
            Merged ValidationResult from all four passes.
        """
        combined = ValidationResult()
        combined.merge(self.validate_structure(path))
        combined.merge(self.validate_manifest(path))
        combined.merge(self.validate_security(path))
        combined.merge(self.validate_tests(path))
        return combined


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _is_semver(version: str) -> bool:
    """Return True if version matches x.y.z semver pattern."""
    parts = version.split(".")
    if len(parts) != 3:
        return False
    return all(p.isdigit() for p in parts)


def _ast_call_to_str(node: ast.expr) -> str:
    """Convert an AST function/attribute expression to a dotted string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _ast_call_to_str(node.value)
        return f"{parent}.{node.attr}"
    return ""
