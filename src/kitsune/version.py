"""Shared package version and source fingerprint for bridge compatibility."""

import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re


PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent.parent


def package_version() -> str:
    try:
        return version("kitsune-mcp")
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
    paths = [PACKAGE_DIR / name for name in ("bridge.py", "bridge_control.py", "version.py")]
    extension = SOURCE_ROOT / "extension"
    if extension.is_dir():
        paths += sorted(p for p in extension.rglob("*") if p.suffix in {".js", ".json"})
    for path in paths:
        digest.update(str(path.relative_to(SOURCE_ROOT)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"{PACKAGE_VERSION}+{digest.hexdigest()[:16]}"


PACKAGE_VERSION = package_version()
BRIDGE_BUILD = bridge_build()
