"""Tests for the EAP CLI tool.

Coverage:
  - Plugin scaffolding (eap plugin new)
  - Plugin validation - structure checks
  - Plugin validation - manifest checks
  - Plugin validation - security AST scan
  - Plugin packaging (eap plugin pack)
  - CLI argument parsing
  - PluginPackager.verify_package
"""

from __future__ import annotations

import argparse
import sys
import textwrap
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Add tools/eap-cli to sys.path so we can import cli/validator/packager
# The directory name has a hyphen which is not a valid Python package name,
# so we add it directly to sys.path for import.
_EAP_CLI_DIR = Path(__file__).parent.parent / "tools" / "eap-cli"
if str(_EAP_CLI_DIR) not in sys.path:
    sys.path.insert(0, str(_EAP_CLI_DIR))

from cli import (  # noqa: E402
    build_parser,
    cmd_plugin_new,
    cmd_plugin_pack,
    cmd_plugin_validate,
    main,
)
from packager import PluginPackager  # noqa: E402
from validator import (  # noqa: E402
    PluginValidator,
    ValidationResult,
    BLOCKED_MODULES,
    BLOCKED_CALLS,
    REQUIRED_FILES,
    REQUIRED_MANIFEST_FIELDS,
)


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def tmp_plugin_dir(tmp_path: Path) -> Path:
    """Create a minimal valid plugin directory for testing."""
    plugin_dir = tmp_path / "test-plugin"
    plugin_dir.mkdir()

    # Required files
    (plugin_dir / "__init__.py").write_text("# test plugin init\n")
    (plugin_dir / "plugin.py").write_text(
        textwrap.dedent(
            """\
            from src.plugins.base import BasePlugin, PluginContext, PluginHook, PluginMetadata
            from typing import Any

            class TestPlugin(BasePlugin):
                @property
                def metadata(self):
                    return PluginMetadata(
                        name="test-plugin",
                        version="1.0.0",
                        author="Test Author",
                        description="A test plugin.",
                        required_permissions=["agent.read"],
                        compatible_versions=["0.1.0"],
                    )

                async def on_load(self, context: PluginContext) -> None:
                    pass

                async def on_unload(self) -> None:
                    pass

                async def handle_hook(self, hook: PluginHook, data: dict[str, Any]) -> dict[str, Any]:
                    return data
            """
        )
    )
    (plugin_dir / "plugin.yaml").write_text(
        textwrap.dedent(
            """\
            name: test-plugin
            version: 1.0.0
            description: A test plugin for testing.
            author: Test Author
            min_platform_version: 0.1.0
            required_permissions:
              - agent.read
            tool_definitions: []
            """
        )
    )

    # Tests directory
    tests_dir = plugin_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_plugin.py").write_text(
        textwrap.dedent(
            """\
            def test_always_passes():
                assert True
            """
        )
    )

    return plugin_dir


@pytest.fixture
def validator() -> PluginValidator:
    return PluginValidator()


@pytest.fixture
def packager() -> PluginPackager:
    return PluginPackager()


# ------------------------------------------------------------------ #
# ValidationResult tests
# ------------------------------------------------------------------ #


class TestValidationResult:
    """Tests for ValidationResult data class."""

    def test_passed_true_when_no_errors(self) -> None:
        result = ValidationResult()
        assert result.passed is True

    def test_passed_false_when_errors(self) -> None:
        result = ValidationResult(errors=["an error"])
        assert result.passed is False

    def test_merge_combines_errors(self) -> None:
        a = ValidationResult(errors=["error1"])
        b = ValidationResult(errors=["error2"])
        a.merge(b)
        assert "error1" in a.errors
        assert "error2" in a.errors

    def test_merge_combines_warnings(self) -> None:
        a = ValidationResult(warnings=["warn1"])
        b = ValidationResult(warnings=["warn2"])
        a.merge(b)
        assert "warn1" in a.warnings
        assert "warn2" in a.warnings

    def test_merge_returns_self(self) -> None:
        a = ValidationResult()
        b = ValidationResult()
        result = a.merge(b)
        assert result is a


# ------------------------------------------------------------------ #
# Structure validation tests
# ------------------------------------------------------------------ #


class TestPluginStructureValidation:
    """Tests for validate_structure."""

    def test_valid_structure_passes(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """A valid plugin directory should pass structure validation."""
        result = validator.validate_structure(str(tmp_plugin_dir))
        assert result.passed, f"Unexpected errors: {result.errors}"

    def test_missing_plugin_py_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Missing plugin.py should fail structure validation."""
        (tmp_plugin_dir / "plugin.py").unlink()
        result = validator.validate_structure(str(tmp_plugin_dir))
        assert not result.passed
        assert any("plugin.py" in e for e in result.errors)

    def test_missing_plugin_yaml_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Missing plugin.yaml should fail structure validation."""
        (tmp_plugin_dir / "plugin.yaml").unlink()
        result = validator.validate_structure(str(tmp_plugin_dir))
        assert not result.passed
        assert any("plugin.yaml" in e for e in result.errors)

    def test_missing_init_py_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Missing __init__.py should fail structure validation."""
        (tmp_plugin_dir / "__init__.py").unlink()
        result = validator.validate_structure(str(tmp_plugin_dir))
        assert not result.passed
        assert any("__init__.py" in e for e in result.errors)

    def test_missing_tests_dir_warns(
        self, validator: PluginValidator, tmp_path: Path
    ) -> None:
        """Missing tests/ directory should produce a warning, not an error."""
        plugin_dir = tmp_path / "no-tests-plugin"
        plugin_dir.mkdir()
        for f in REQUIRED_FILES:
            (plugin_dir / f).write_text("# placeholder\n")

        result = validator.validate_structure(str(plugin_dir))
        assert result.passed  # no errors
        assert any("tests" in w.lower() for w in result.warnings)

    def test_nonexistent_path_fails(self, validator: PluginValidator) -> None:
        """A non-existent path should fail with an error."""
        result = validator.validate_structure("/nonexistent/path/xyz")
        assert not result.passed


# ------------------------------------------------------------------ #
# Manifest validation tests
# ------------------------------------------------------------------ #


class TestPluginManifestValidation:
    """Tests for validate_manifest."""

    def test_valid_manifest_passes(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """A valid plugin.yaml should pass manifest validation."""
        result = validator.validate_manifest(str(tmp_plugin_dir))
        assert result.passed, f"Unexpected errors: {result.errors}"

    def test_missing_name_field_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """A manifest without 'name' should fail."""
        (tmp_plugin_dir / "plugin.yaml").write_text(
            "version: 1.0.0\ndescription: test\nauthor: Me\nmin_platform_version: 0.1.0\n"
        )
        result = validator.validate_manifest(str(tmp_plugin_dir))
        assert not result.passed
        assert any("name" in e for e in result.errors)

    def test_invalid_version_semver_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """A non-semver version should fail."""
        (tmp_plugin_dir / "plugin.yaml").write_text(
            "name: test\nversion: not-semver\ndescription: test\nauthor: Me\nmin_platform_version: 0.1.0\n"
        )
        result = validator.validate_manifest(str(tmp_plugin_dir))
        assert not result.passed
        assert any("semver" in e.lower() for e in result.errors)

    def test_all_required_fields_present(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Manifest should require all documented required fields."""
        # Verify our test fixture has all required fields
        result = validator.validate_manifest(str(tmp_plugin_dir))
        assert result.passed

    def test_missing_manifest_file_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Missing plugin.yaml should fail."""
        (tmp_plugin_dir / "plugin.yaml").unlink()
        result = validator.validate_manifest(str(tmp_plugin_dir))
        assert not result.passed


# ------------------------------------------------------------------ #
# Security validation tests
# ------------------------------------------------------------------ #


class TestPluginSecurityValidation:
    """Tests for validate_security (AST-based)."""

    def test_safe_plugin_passes(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """A plugin with no dangerous imports should pass security validation."""
        result = validator.validate_security(str(tmp_plugin_dir))
        assert result.passed, f"Unexpected errors: {result.errors}"

    def test_subprocess_import_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Importing subprocess should fail security validation."""
        (tmp_plugin_dir / "plugin.py").write_text(
            "import subprocess\n\nclass MyPlugin:\n    pass\n"
        )
        result = validator.validate_security(str(tmp_plugin_dir))
        assert not result.passed
        assert any("subprocess" in e for e in result.errors)

    def test_os_system_call_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Calling os.system() should fail security validation."""
        (tmp_plugin_dir / "plugin.py").write_text(
            "import os\n\nclass MyPlugin:\n    def run(self):\n        os.system('ls')\n"
        )
        result = validator.validate_security(str(tmp_plugin_dir))
        assert not result.passed
        assert any("os.system" in e for e in result.errors)

    def test_eval_call_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Calling eval() should fail security validation."""
        (tmp_plugin_dir / "plugin.py").write_text(
            "class MyPlugin:\n    def run(self, code):\n        return eval(code)\n"
        )
        result = validator.validate_security(str(tmp_plugin_dir))
        assert not result.passed
        assert any("eval" in e for e in result.errors)

    def test_exec_call_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Calling exec() should fail security validation."""
        (tmp_plugin_dir / "plugin.py").write_text(
            "class MyPlugin:\n    def run(self, code):\n        exec(code)\n"
        )
        result = validator.validate_security(str(tmp_plugin_dir))
        assert not result.passed
        assert any("exec" in e for e in result.errors)

    def test_dunder_builtins_access_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """Accessing __builtins__ should fail security validation."""
        (tmp_plugin_dir / "plugin.py").write_text(
            "class MyPlugin:\n    def escape(self):\n        return self.__builtins__\n"
        )
        result = validator.validate_security(str(tmp_plugin_dir))
        assert not result.passed

    def test_syntax_error_fails(
        self, validator: PluginValidator, tmp_plugin_dir: Path
    ) -> None:
        """A file with a Python syntax error should fail security validation."""
        (tmp_plugin_dir / "plugin.py").write_text("def broken(\n")
        result = validator.validate_security(str(tmp_plugin_dir))
        assert not result.passed
        assert any("syntax" in e.lower() for e in result.errors)

    def test_blocked_modules_set(self) -> None:
        """BLOCKED_MODULES should contain the documented dangerous modules."""
        assert "subprocess" in BLOCKED_MODULES
        assert "ctypes" in BLOCKED_MODULES

    def test_blocked_calls_set(self) -> None:
        """BLOCKED_CALLS should contain the documented dangerous calls."""
        assert "os.system" in BLOCKED_CALLS
        assert "eval" in BLOCKED_CALLS
        assert "exec" in BLOCKED_CALLS


# ------------------------------------------------------------------ #
# Packaging tests
# ------------------------------------------------------------------ #


class TestPluginPackaging:
    """Tests for PluginPackager."""

    def test_pack_creates_zip(
        self, packager: PluginPackager, tmp_plugin_dir: Path, tmp_path: Path
    ) -> None:
        """pack() should create a zip archive in the output directory."""
        output_dir = tmp_path / "dist"
        zip_path = packager.pack(tmp_plugin_dir, output_dir)
        assert zip_path.exists()
        assert zip_path.suffix == ".zip"

    def test_pack_zip_name_from_manifest(
        self, packager: PluginPackager, tmp_plugin_dir: Path, tmp_path: Path
    ) -> None:
        """Zip filename should be derived from plugin name and version."""
        output_dir = tmp_path / "dist"
        zip_path = packager.pack(tmp_plugin_dir, output_dir)
        assert "test-plugin" in zip_path.name
        assert "1.0.0" in zip_path.name

    def test_pack_includes_required_files(
        self, packager: PluginPackager, tmp_plugin_dir: Path, tmp_path: Path
    ) -> None:
        """Archive should contain plugin.py and plugin.yaml."""
        output_dir = tmp_path / "dist"
        zip_path = packager.pack(tmp_plugin_dir, output_dir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())

        assert "plugin.py" in names
        assert "plugin.yaml" in names
        assert "__init__.py" in names

    def test_pack_includes_checksum(
        self, packager: PluginPackager, tmp_plugin_dir: Path, tmp_path: Path
    ) -> None:
        """Archive should contain a CHECKSUM.sha256 file."""
        output_dir = tmp_path / "dist"
        zip_path = packager.pack(tmp_plugin_dir, output_dir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())

        assert "CHECKSUM.sha256" in names

    def test_pack_excludes_pycache(
        self, packager: PluginPackager, tmp_plugin_dir: Path, tmp_path: Path
    ) -> None:
        """Archive should not contain __pycache__ files."""
        pycache = tmp_plugin_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "plugin.cpython-312.pyc").write_bytes(b"bytecode")

        output_dir = tmp_path / "dist"
        zip_path = packager.pack(tmp_plugin_dir, output_dir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())

        assert not any("__pycache__" in n for n in names)

    def test_verify_package_valid_archive(
        self, packager: PluginPackager, tmp_plugin_dir: Path, tmp_path: Path
    ) -> None:
        """verify_package should return True for a freshly packed archive."""
        output_dir = tmp_path / "dist"
        zip_path = packager.pack(tmp_plugin_dir, output_dir)
        ok, errors = packager.verify_package(zip_path)
        assert ok is True
        assert errors == []

    def test_verify_package_missing_file_fails(
        self, packager: PluginPackager, tmp_plugin_dir: Path, tmp_path: Path
    ) -> None:
        """Removing a file listed in the checksum should fail verification."""
        output_dir = tmp_path / "dist"
        zip_path = packager.pack(tmp_plugin_dir, output_dir)

        # Tamper: remove plugin.py from the zip
        tampered_path = tmp_path / "tampered.zip"
        with zipfile.ZipFile(zip_path, "r") as zf_in:
            with zipfile.ZipFile(tampered_path, "w") as zf_out:
                for item in zf_in.infolist():
                    if item.filename != "plugin.py":
                        zf_out.writestr(item, zf_in.read(item.filename))

        ok, errors = packager.verify_package(tampered_path)
        assert ok is False
        assert len(errors) > 0

    def test_verify_nonexistent_archive_fails(
        self, packager: PluginPackager, tmp_path: Path
    ) -> None:
        """Verifying a non-existent archive should return failure."""
        ok, errors = packager.verify_package(tmp_path / "missing.zip")
        assert ok is False
        assert errors

    def test_pack_nonexistent_dir_raises(
        self, packager: PluginPackager, tmp_path: Path
    ) -> None:
        """Packing a non-existent directory should raise ValueError."""
        with pytest.raises(ValueError, match="does not exist"):
            packager.pack(tmp_path / "nonexistent", tmp_path / "dist")


# ------------------------------------------------------------------ #
# CLI argument parsing tests
# ------------------------------------------------------------------ #


class TestCLIArgumentParsing:
    """Tests for CLI argument parser."""

    def test_parser_creates_successfully(self) -> None:
        """build_parser should return an ArgumentParser without error."""
        parser = build_parser()
        assert isinstance(parser, argparse.ArgumentParser)

    def test_parse_plugin_new(self) -> None:
        """eap plugin new <name> should parse correctly."""
        parser = build_parser()
        args = parser.parse_args(["plugin", "new", "my-plugin"])
        assert args.command == "plugin"
        assert args.plugin_command == "new"
        assert args.name == "my-plugin"

    def test_parse_plugin_new_with_output(self) -> None:
        """eap plugin new <name> --output <dir> should parse correctly."""
        parser = build_parser()
        args = parser.parse_args(["plugin", "new", "my-plugin", "--output", "/tmp"])
        assert args.output == "/tmp"

    def test_parse_plugin_validate(self) -> None:
        """eap plugin validate <path> should parse correctly."""
        parser = build_parser()
        args = parser.parse_args(["plugin", "validate", "./my-plugin"])
        assert args.command == "plugin"
        assert args.plugin_command == "validate"
        assert args.path == "./my-plugin"

    def test_parse_plugin_pack(self) -> None:
        """eap plugin pack <path> should parse correctly."""
        parser = build_parser()
        args = parser.parse_args(["plugin", "pack", "./my-plugin"])
        assert args.command == "plugin"
        assert args.plugin_command == "pack"
        assert args.path == "./my-plugin"

    def test_parse_plugin_pack_with_output(self) -> None:
        """eap plugin pack <path> --output <dir> should parse correctly."""
        parser = build_parser()
        args = parser.parse_args(["plugin", "pack", "./my-plugin", "--output", "./dist"])
        assert args.output == "./dist"

    def test_parse_plugin_test(self) -> None:
        """eap plugin test <path> should parse correctly."""
        parser = build_parser()
        args = parser.parse_args(["plugin", "test", "./my-plugin"])
        assert args.command == "plugin"
        assert args.plugin_command == "test"
        assert args.path == "./my-plugin"

    def test_parse_plugin_list(self) -> None:
        """eap plugin list should parse correctly."""
        parser = build_parser()
        args = parser.parse_args(["plugin", "list"])
        assert args.command == "plugin"
        assert args.plugin_command == "list"

    def test_parse_dev_start(self) -> None:
        """eap dev start should parse correctly."""
        parser = build_parser()
        args = parser.parse_args(["dev", "start"])
        assert args.command == "dev"
        assert args.dev_command == "start"

    def test_parse_dev_seed(self) -> None:
        """eap dev seed should parse correctly."""
        parser = build_parser()
        args = parser.parse_args(["dev", "seed"])
        assert args.command == "dev"
        assert args.dev_command == "seed"

    def test_main_no_args_returns_zero(self) -> None:
        """Running main with no args should print help and return 0."""
        result = main([])
        assert result == 0


# ------------------------------------------------------------------ #
# Plugin scaffolding tests
# ------------------------------------------------------------------ #


class TestPluginScaffolding:
    """Tests for eap plugin new command."""

    def test_scaffolding_creates_directory(self, tmp_path: Path) -> None:
        """eap plugin new should create the plugin directory."""
        args = argparse.Namespace(name="my-new-plugin", output=str(tmp_path))
        result = cmd_plugin_new(args)
        assert result == 0
        assert (tmp_path / "my-new-plugin").is_dir()

    def test_scaffolding_creates_required_files(self, tmp_path: Path) -> None:
        """Scaffolded plugin should have all required files."""
        args = argparse.Namespace(name="my-new-plugin", output=str(tmp_path))
        cmd_plugin_new(args)
        plugin_dir = tmp_path / "my-new-plugin"

        for fname in REQUIRED_FILES:
            assert (plugin_dir / fname).is_file(), f"Missing: {fname}"

    def test_scaffolding_substitutes_name(self, tmp_path: Path) -> None:
        """Scaffolded files should contain the plugin name, not the placeholder."""
        args = argparse.Namespace(name="awesome-plugin", output=str(tmp_path))
        cmd_plugin_new(args)
        plugin_dir = tmp_path / "awesome-plugin"

        manifest_content = (plugin_dir / "plugin.yaml").read_text()
        assert "awesome-plugin" in manifest_content
        assert "__PLUGIN_NAME__" not in manifest_content

    def test_scaffolding_fails_if_dir_exists(self, tmp_path: Path) -> None:
        """Scaffolding into an existing directory should fail gracefully."""
        existing = tmp_path / "existing-plugin"
        existing.mkdir()
        args = argparse.Namespace(name="existing-plugin", output=str(tmp_path))
        result = cmd_plugin_new(args)
        assert result == 1
