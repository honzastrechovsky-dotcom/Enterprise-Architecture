"""EAP CLI tool - Enterprise Agent Platform developer toolkit.

Phase 11B: Plugin Dev Kit.

No external dependencies - uses only Python stdlib.

Commands::

    eap plugin new <name>        - Scaffold new plugin from template
    eap plugin test <path>       - Run plugin test suite
    eap plugin validate <path>   - Validate plugin structure and security
    eap plugin pack <path>       - Package plugin for distribution
    eap plugin list              - List installed plugins
    eap dev start                - Start development environment
    eap dev seed                 - Seed development data

Usage::

    python -m tools.eap-cli.cli plugin new my-plugin
    python -m tools.eap-cli.cli plugin validate ./my-plugin
    python -m tools.eap-cli.cli plugin pack ./my-plugin --output ./dist/
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# Locate the templates directory relative to this file
_TOOL_DIR = Path(__file__).parent
_TEMPLATES_DIR = _TOOL_DIR / "templates"

# ------------------------------------------------------------------ #
# Formatting helpers
# ------------------------------------------------------------------ #

_RESET = "\033[0m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"


def _ok(msg: str) -> None:
    print(f"{_GREEN}  [OK]{_RESET}  {msg}")


def _warn(msg: str) -> None:
    print(f"{_YELLOW} [WARN]{_RESET} {msg}")


def _err(msg: str) -> None:
    print(f"{_RED}[ERROR]{_RESET} {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"{_CYAN} [INFO]{_RESET} {msg}")


def _header(msg: str) -> None:
    print(f"\n{_BOLD}{msg}{_RESET}")


# ------------------------------------------------------------------ #
# Plugin commands
# ------------------------------------------------------------------ #


def cmd_plugin_new(args: argparse.Namespace) -> int:
    """Scaffold a new plugin from the built-in template."""
    name: str = args.name
    output_dir = Path(args.output) if args.output else Path.cwd()
    plugin_dir = output_dir / name

    if plugin_dir.exists():
        _err(f"Directory already exists: {plugin_dir}")
        return 1

    template_dir = _TEMPLATES_DIR / "plugin_template"
    if not template_dir.is_dir():
        _err(f"Plugin template not found at: {template_dir}")
        _info("Expected template directory: tools/eap-cli/templates/plugin_template/")
        return 1

    _header(f"Scaffolding new plugin: {name!r}")

    # Copy template tree to new plugin directory
    shutil.copytree(str(template_dir), str(plugin_dir))

    # Perform name substitutions in templated files
    _substitute_template_vars(plugin_dir, name)

    _ok(f"Plugin directory created: {plugin_dir}")
    _info("Next steps:")
    print(f"   1. cd {plugin_dir}")
    print("   2. Edit plugin.yaml to set your metadata")
    print("   3. Implement your plugin in plugin.py")
    print("   4. Add tests in tests/test_plugin.py")
    print(f"   5. eap plugin validate {plugin_dir}")
    print(f"   6. eap plugin pack {plugin_dir}")
    return 0


def _substitute_template_vars(plugin_dir: Path, plugin_name: str) -> None:
    """Replace __PLUGIN_NAME__ and __PLUGIN_CLASS__ in template files."""
    class_name = "".join(w.capitalize() for w in plugin_name.replace("-", "_").split("_"))
    substitutions = {
        "__PLUGIN_NAME__": plugin_name,
        "__PLUGIN_CLASS__": f"{class_name}Plugin",
        "__PLUGIN_DESCRIPTION__": f"The {plugin_name} plugin for Enterprise Agent Platform.",
    }

    for path in plugin_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".yaml", ".md", ".j2", ".txt"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
            for placeholder, value in substitutions.items():
                content = content.replace(placeholder, value)
            path.write_text(content, encoding="utf-8")
        except OSError:
            pass  # Skip binary or unreadable files


def cmd_plugin_test(args: argparse.Namespace) -> int:
    """Run the plugin test suite using pytest."""
    plugin_path = Path(args.path).resolve()
    tests_dir = plugin_path / "tests"

    if not plugin_path.is_dir():
        _err(f"Plugin directory not found: {plugin_path}")
        return 1

    if not tests_dir.is_dir():
        _err(f"No tests/ directory found in: {plugin_path}")
        return 1

    _header(f"Running tests for plugin: {plugin_path.name}")

    pytest_args = [sys.executable, "-m", "pytest", str(tests_dir), "-v"]
    if args.verbose:
        pytest_args.append("--tb=long")
    else:
        pytest_args.append("--tb=short")

    result = subprocess.run(pytest_args)
    return result.returncode


def cmd_plugin_validate(args: argparse.Namespace) -> int:
    """Validate plugin structure, manifest, security, and tests."""
    # Import relative to the cli module's directory (works whether invoked as
    # 'eap' CLI tool or imported by tests that added tools/eap-cli to sys.path).
    try:
        from validator import PluginValidator  # type: ignore[import]  # noqa: PLC0415
    except ImportError:
        from tools.eap_cli.validator import PluginValidator  # noqa: PLC0415

    plugin_path = Path(args.path).resolve()

    _header(f"Validating plugin: {plugin_path.name}")

    validator = PluginValidator()
    checks = [
        ("Structure", validator.validate_structure),
        ("Manifest", validator.validate_manifest),
        ("Security", validator.validate_security),
        ("Tests", validator.validate_tests),
    ]

    total_errors = 0
    total_warnings = 0

    for check_name, check_fn in checks:
        print(f"\n  Checking {check_name}...")
        result = check_fn(str(plugin_path))
        for error in result.errors:
            _err(f"  {error}")
        for warning in result.warnings:
            _warn(f"  {warning}")
        if result.passed:
            _ok(f"{check_name} check passed")
        else:
            _err(f"{check_name} check FAILED ({len(result.errors)} error(s))")
        total_errors += len(result.errors)
        total_warnings += len(result.warnings)

    print()
    if total_errors == 0:
        _ok(f"All checks passed ({total_warnings} warning(s))")
        return 0
    else:
        _err(f"Validation failed: {total_errors} error(s), {total_warnings} warning(s)")
        return 1


def cmd_plugin_pack(args: argparse.Namespace) -> int:
    """Package a plugin into a distributable zip archive."""
    try:
        from packager import PluginPackager  # type: ignore[import]  # noqa: PLC0415
    except ImportError:
        from tools.eap_cli.packager import PluginPackager  # noqa: PLC0415

    plugin_path = Path(args.path).resolve()
    output_path = Path(args.output).resolve() if args.output else plugin_path.parent

    _header(f"Packaging plugin: {plugin_path.name}")

    packager = PluginPackager()
    try:
        zip_path = packager.pack(plugin_path, output_path)
    except ValueError as exc:
        _err(str(exc))
        return 1

    _ok(f"Plugin packaged: {zip_path}")

    # Verify immediately
    _info("Verifying package integrity...")
    ok, errors = packager.verify_package(zip_path)
    if ok:
        _ok("Package integrity verified")
    else:
        for error in errors:
            _err(error)
        return 1

    return 0


def cmd_plugin_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    """List installed plugins from the local registry."""
    # Look for plugins in the src/plugins/ directory relative to project root
    project_root = _find_project_root()
    if project_root is None:
        _err("Could not locate project root (no pyproject.toml found)")
        return 1

    plugins_dir = project_root / "src" / "plugins" / "examples"
    local_plugins_dir = project_root / "plugins"

    search_dirs = [d for d in [plugins_dir, local_plugins_dir] if d.is_dir()]

    if not search_dirs:
        _info("No plugin directories found.")
        _info("Plugins are discovered from: src/plugins/examples/, plugins/")
        return 0

    _header("Installed Plugins")

    found = 0
    for search_dir in search_dirs:
        for plugin_yaml in search_dir.rglob("plugin.yaml"):
            plugin_dir = plugin_yaml.parent
            name = version = description = "unknown"
            try:
                for line in plugin_yaml.read_text(encoding="utf-8").splitlines():
                    if line.startswith("name:"):
                        name = line.partition(":")[2].strip()
                    elif line.startswith("version:"):
                        version = line.partition(":")[2].strip()
                    elif line.startswith("description:"):
                        description = line.partition(":")[2].strip()
            except OSError:
                pass
            print(f"  {_BOLD}{name}{_RESET} v{version}")
            print(f"    {description}")
            print(f"    Path: {plugin_dir}")
            found += 1

    if found == 0:
        _info("No plugins installed.")
        _info("Scaffold a new plugin with: eap plugin new <name>")

    return 0


# ------------------------------------------------------------------ #
# Dev commands
# ------------------------------------------------------------------ #


def cmd_dev_start(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Start the development environment using docker-compose."""
    project_root = _find_project_root()
    if project_root is None:
        _err("Cannot find project root (no pyproject.toml found).")
        return 1

    compose_file = project_root / "docker-compose.dev.yml"
    if not compose_file.is_file():
        compose_file = project_root / "docker-compose.yml"

    if not compose_file.is_file():
        _err("No docker-compose.yml or docker-compose.dev.yml found.")
        return 1

    _header("Starting development environment")
    _info(f"Using: {compose_file.name}")

    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d"],
        cwd=str(project_root),
    )

    if result.returncode == 0:
        _ok("Development environment started")
        _info("API server: http://localhost:8000")
        _info("LiteLLM proxy: http://localhost:4000")
        _info("Keycloak: http://localhost:8080")
        _info("Postgres: localhost:5432")
    else:
        _err("Failed to start development environment")

    return result.returncode


def cmd_dev_seed(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Seed the development database with test data."""
    project_root = _find_project_root()
    if project_root is None:
        _err("Cannot find project root.")
        return 1

    seed_script = project_root / "scripts" / "seed_dev_data.py"

    _header("Seeding development data")

    if not seed_script.is_file():
        # Generate minimal seed data inline
        _info("No seed script found at scripts/seed_dev_data.py")
        _info("Creating minimal development seed data...")
        _create_inline_seed(project_root)
        return 0

    result = subprocess.run(
        [sys.executable, str(seed_script)],
        cwd=str(project_root),
    )
    if result.returncode == 0:
        _ok("Development data seeded successfully")
    else:
        _err("Seeding failed")

    return result.returncode


def _create_inline_seed(project_root: Path) -> None:
    """Create a minimal inline seed script and run it."""
    seed_code = textwrap.dedent(
        """\
        #!/usr/bin/env python
        \"\"\"Minimal development data seed.\"\"\"
        import asyncio
        import uuid

        async def seed() -> None:
            print("  Creating default tenant...")
            # Add your seed logic here
            print("  Seed complete.")

        asyncio.run(seed())
        """
    )
    scripts_dir = project_root / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    seed_path = scripts_dir / "seed_dev_data.py"
    seed_path.write_text(seed_code, encoding="utf-8")
    _info(f"Created: {seed_path}")

    result = subprocess.run([sys.executable, str(seed_path)], cwd=str(project_root))
    if result.returncode == 0:
        _ok("Seed complete")
    else:
        _err("Seed script failed")


# ------------------------------------------------------------------ #
# Argument parser
# ------------------------------------------------------------------ #


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser tree."""
    parser = argparse.ArgumentParser(
        prog="eap",
        description="Enterprise Agent Platform developer CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              eap plugin new my-analytics-plugin
              eap plugin validate ./my-analytics-plugin
              eap plugin test ./my-analytics-plugin
              eap plugin pack ./my-analytics-plugin --output ./dist/
              eap plugin list
              eap dev start
              eap dev seed
            """
        ),
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # ---------------------------------------------------------------- #
    # eap plugin
    # ---------------------------------------------------------------- #
    plugin_parser = subparsers.add_parser("plugin", help="Plugin management commands")
    plugin_subparsers = plugin_parser.add_subparsers(
        dest="plugin_command", metavar="SUBCOMMAND"
    )

    # eap plugin new
    new_parser = plugin_subparsers.add_parser(
        "new", help="Scaffold a new plugin from template"
    )
    new_parser.add_argument("name", help="Plugin name (use lowercase-hyphenated format)")
    new_parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output directory (default: current directory)",
    )

    # eap plugin test
    test_parser = plugin_subparsers.add_parser(
        "test", help="Run the plugin test suite"
    )
    test_parser.add_argument("path", help="Path to plugin directory")
    test_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose test output"
    )

    # eap plugin validate
    validate_parser = plugin_subparsers.add_parser(
        "validate", help="Validate plugin structure and security"
    )
    validate_parser.add_argument("path", help="Path to plugin directory")

    # eap plugin pack
    pack_parser = plugin_subparsers.add_parser(
        "pack", help="Package plugin for distribution"
    )
    pack_parser.add_argument("path", help="Path to plugin directory")
    pack_parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output directory for the zip archive (default: parent of plugin dir)",
    )

    # eap plugin list
    plugin_subparsers.add_parser("list", help="List installed plugins")

    # ---------------------------------------------------------------- #
    # eap dev
    # ---------------------------------------------------------------- #
    dev_parser = subparsers.add_parser("dev", help="Development environment commands")
    dev_subparsers = dev_parser.add_subparsers(dest="dev_command", metavar="SUBCOMMAND")

    dev_subparsers.add_parser("start", help="Start the development environment")
    dev_subparsers.add_parser("seed", help="Seed development data")

    return parser


# ------------------------------------------------------------------ #
# Dispatch
# ------------------------------------------------------------------ #


def main(argv: list[str] | None = None) -> int:
    """Entry point for the EAP CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "plugin":
        if args.plugin_command == "new":
            return cmd_plugin_new(args)
        elif args.plugin_command == "test":
            return cmd_plugin_test(args)
        elif args.plugin_command == "validate":
            return cmd_plugin_validate(args)
        elif args.plugin_command == "pack":
            return cmd_plugin_pack(args)
        elif args.plugin_command == "list":
            return cmd_plugin_list(args)
        else:
            parser.parse_args(["plugin", "--help"])
            return 1

    elif args.command == "dev":
        if args.dev_command == "start":
            return cmd_dev_start(args)
        elif args.dev_command == "seed":
            return cmd_dev_seed(args)
        else:
            parser.parse_args(["dev", "--help"])
            return 1

    else:
        parser.print_help()
        return 0


# ------------------------------------------------------------------ #
# Utilities
# ------------------------------------------------------------------ #


def _find_project_root() -> Path | None:
    """Walk up the directory tree to find the project root (pyproject.toml)."""
    current = Path.cwd()
    for _ in range(10):
        if (current / "pyproject.toml").is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent

    # Also check relative to this file's location
    candidate = _TOOL_DIR.parent.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate

    return None


if __name__ == "__main__":
    sys.exit(main())
