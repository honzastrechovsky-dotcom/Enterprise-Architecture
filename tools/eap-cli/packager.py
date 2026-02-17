"""Plugin packaging for distribution.

Phase 11B: Plugin Dev Kit.

``PluginPackager`` creates distributable zip archives from plugin directories.

The archive structure::

    my-plugin-1.0.0.zip
    ├── plugin.py
    ├── plugin.yaml
    ├── __init__.py
    ├── README.md        (if present)
    ├── tests/
    │   └── test_plugin.py
    └── CHECKSUM.sha256  (generated)

Excluded from archives:
  - __pycache__/ directories
  - .git/ directories
  - .env files
  - *.pyc compiled files
  - .DS_Store / Thumbs.db

Usage::

    packager = PluginPackager()
    zip_path = packager.pack("/path/to/my-plugin", "/path/to/output/")
    ok, errors = packager.verify_package(zip_path)
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Patterns to exclude from the package archive
_EXCLUDE_DIRS = frozenset({"__pycache__", ".git", ".tox", ".venv", "venv", "node_modules"})
_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo", ".env", ".DS_Store"})
_EXCLUDE_NAMES = frozenset({".env", "Thumbs.db", ".gitignore", ".gitkeep"})


class PluginPackager:
    """Create and verify distributable plugin zip archives."""

    # ---------------------------------------------------------------- #
    # Packing
    # ---------------------------------------------------------------- #

    def pack(self, plugin_path: str | Path, output_path: str | Path) -> Path:
        """Create a distributable zip archive of the plugin.

        The output filename is derived from the plugin manifest:
        ``{name}-{version}.zip``

        If no manifest is found, falls back to the directory name.

        Args:
            plugin_path: Path to the plugin root directory.
            output_path: Directory (or exact file path) for the output zip.

        Returns:
            Path to the created zip archive.

        Raises:
            ValueError: If the plugin directory does not exist.
            OSError:    If the output directory cannot be created.
        """
        plugin_dir = Path(plugin_path).resolve()

        if not plugin_dir.is_dir():
            raise ValueError(f"Plugin directory does not exist: {plugin_dir}")

        # Derive archive name from manifest
        archive_name = self._derive_archive_name(plugin_dir)

        output_dir = Path(output_path).resolve()
        if output_dir.suffix == ".zip":
            # Caller provided an explicit zip path
            zip_path = output_dir
            zip_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            zip_path = output_dir / archive_name

        # Collect files to include
        files_to_pack = self._collect_files(plugin_dir)

        # Build checksum manifest in memory
        checksum_lines: list[str] = []

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for abs_path in sorted(files_to_pack):
                rel_path = abs_path.relative_to(plugin_dir)
                data = abs_path.read_bytes()
                sha256 = hashlib.sha256(data).hexdigest()
                checksum_lines.append(f"{sha256}  {rel_path}")
                zf.writestr(str(rel_path), data)

            # Write CHECKSUM.sha256 as the last entry
            checksum_content = "\n".join(sorted(checksum_lines)) + "\n"
            zf.writestr("CHECKSUM.sha256", checksum_content.encode("utf-8"))

        log.info(
            "plugin.packed",
            plugin=str(plugin_dir),
            archive=str(zip_path),
            file_count=len(files_to_pack),
        )
        return zip_path

    # ---------------------------------------------------------------- #
    # Verification
    # ---------------------------------------------------------------- #

    def verify_package(self, zip_path: str | Path) -> tuple[bool, list[str]]:
        """Verify the integrity of a plugin zip archive.

        Reads the embedded CHECKSUM.sha256 file and verifies each listed
        file against its stored SHA-256 digest.

        Args:
            zip_path: Path to the plugin zip archive.

        Returns:
            A tuple ``(ok, errors)`` where ``ok`` is True if all checksums
            match and ``errors`` is a list of failure messages.
        """
        zip_path = Path(zip_path)
        errors: list[str] = []

        if not zip_path.is_file():
            return False, [f"Archive not found: {zip_path}"]

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = set(zf.namelist())

                if "CHECKSUM.sha256" not in names:
                    return False, ["Archive is missing CHECKSUM.sha256"]

                checksum_data = zf.read("CHECKSUM.sha256").decode("utf-8")
                expected: dict[str, str] = {}

                for line in checksum_data.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        digest, filename = line.split("  ", 1)
                        expected[filename] = digest
                    except ValueError:
                        errors.append(f"Malformed checksum line: {line!r}")

                for filename, digest in expected.items():
                    if filename not in names:
                        errors.append(f"File listed in checksum but missing from archive: {filename}")
                        continue
                    actual_data = zf.read(filename)
                    actual_digest = hashlib.sha256(actual_data).hexdigest()
                    if actual_digest != digest:
                        errors.append(
                            f"Checksum mismatch for {filename}: "
                            f"expected {digest}, got {actual_digest}"
                        )

        except zipfile.BadZipFile as exc:
            return False, [f"Archive is not a valid zip file: {exc}"]

        ok = len(errors) == 0
        if ok:
            log.info("plugin.package_verified", archive=str(zip_path))
        else:
            log.warning(
                "plugin.package_verification_failed",
                archive=str(zip_path),
                errors=errors,
            )
        return ok, errors

    # ---------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------- #

    def _collect_files(self, plugin_dir: Path) -> list[Path]:
        """Return all files to include in the archive."""
        files: list[Path] = []
        for item in plugin_dir.rglob("*"):
            if not item.is_file():
                continue
            if self._should_exclude(item, plugin_dir):
                continue
            files.append(item)
        return files

    @staticmethod
    def _should_exclude(path: Path, root: Path) -> bool:
        """Return True if the path should be excluded from the archive."""
        # Check if any parent directory is in the exclude list
        for part in path.relative_to(root).parts[:-1]:
            if part in _EXCLUDE_DIRS:
                return True
        # Check file name patterns
        if path.name in _EXCLUDE_NAMES:
            return True
        if path.suffix in _EXCLUDE_SUFFIXES:
            return True
        # Exclude hidden files (dotfiles)
        if path.name.startswith("."):
            return True
        return False

    @staticmethod
    def _derive_archive_name(plugin_dir: Path) -> str:
        """Derive archive filename from plugin.yaml, fallback to dir name."""
        manifest_path = plugin_dir / "plugin.yaml"
        if manifest_path.is_file():
            try:
                text = manifest_path.read_text(encoding="utf-8")
                name = version = ""
                for line in text.splitlines():
                    if line.startswith("name:"):
                        name = line.partition(":")[2].strip()
                    elif line.startswith("version:"):
                        version = line.partition(":")[2].strip()
                if name and version:
                    # Sanitise for filename safety
                    safe_name = name.replace(" ", "-").replace("/", "-")
                    return f"{safe_name}-{version}.zip"
            except OSError:
                pass
        return f"{plugin_dir.name}.zip"
