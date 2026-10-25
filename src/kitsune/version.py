"""Shared package version and source fingerprint for bridge compatibility."""

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re


PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent.parent


def package_version() -> str:
    try:
        return version("kitsune-browser")  # the distribution with the code, not kitsune-browser-mcp
    except PackageNotFoundError:
        # Python 3.10 has no tomllib; only read the project version string.
        try:
            project = (SOURCE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
            section = project.split("[project]", 1)[1].split("\n[", 1)[0]
            match = re.search(r'^version\s*=\s*"([^"]+)"', section, re.MULTILINE)
            if match:
                return match[1]
        except (OSError, IndexError):
            pass
        return "unknown"


def bridge_build() -> str:
    """Fingerprint installed bridge code, plus extension sources in a checkout.

    Compute once at process startup: a running daemon must keep reporting the
    code it loaded, not newly edited files on disk.
    """
    digest = hashlib.sha256()
    paths = [PACKAGE_DIR / name for name in (
        "bridge.py", "bridge_control.py", "capabilities.py", "task_modes.py", "version.py")]
    extension = SOURCE_ROOT / "extension"
    if extension.is_dir():
        paths += sorted(p for p in extension.rglob("*") if p.suffix in {".js", ".json"})
    for path in paths:
        digest.update(str(path.relative_to(SOURCE_ROOT)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"{PACKAGE_VERSION}+{digest.hexdigest()[:16]}"


def extension_source_hash(directory: Path) -> str | None:
    """Fingerprint the scripts an unpacked extension's manifest loads; None if unreadable.

    background.js computes the same sourceHash over the copy the browser loaded, so a
    difference means the developer build was edited but not reloaded.
    """
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        scripts = manifest.get("background", {}).get("scripts", [])
        scripts += [name for entry in manifest.get("content_scripts", []) for name in entry.get("js", [])]
        digest = hashlib.sha256()
        for name in dict.fromkeys(scripts):
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update((directory / name).read_bytes())
            digest.update(b"\0")
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return digest.hexdigest()[:16] if scripts else None


def extension_manifest_hash(directory: Path) -> str | None:
    """Fingerprint an unpacked extension's manifest.json bytes; None if unreadable.

    Separate from extension_source_hash, so extensions that only send that one still compare
    their scripts; background.js computes the same manifestHash.
    """
    try:
        return hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()[:16]
    except OSError:
        return None


PACKAGE_VERSION = package_version()
BRIDGE_BUILD = bridge_build()
