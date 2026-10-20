"""Build the Kitsune browser extension variants.

    python scripts/build_extension.py signed --out build/extension-signed
    python scripts/build_extension.py dev --out build/extension-dev --zip dist/kitsune-extension-dev.zip

signed: the AMO (Mozilla-signed) build. Every `// @kitsune-dev-only:start` ...
        `// @kitsune-dev-only:end` region is removed and KITSUNE_BUILD becomes
        'signed', because Mozilla's add-on policy forbids executing code that is
        not part of the package. The build fails if code execution remains.
        It also gets gecko.update_url so installed copies update from releases.
dev:    the full extension (browser_js etc.), loaded as a temporary add-on.

Both stamp manifest.json with the pyproject version so extension and server match.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension"
START, END = "@kitsune-dev-only:start", "@kitsune-dev-only:end"
BUILD_LINE = "const KITSUNE_BUILD = 'dev';"
BUILD_FILES = ("content/content.js", "background/background.js")
# Update manifest (scripts/update_manifest.py) attached to every GitHub release.
UPDATE_URL = "https://github.com/junyeong-nero/kitsune/releases/latest/download/updates.json"
# Code execution or HTML injection that must not ship in the signed build.
FORBIDDEN = {
    "eval": re.compile(r"\beval\s*\("),
    "Function constructor": re.compile(r"\bnew\s+Function\b"),
    "executeScript with code": re.compile(r"executeScript\([^)]*\bcode\s*:"),
    "innerHTML/outerHTML assignment": re.compile(r"\.(?:inner|outer)HTML\s*="),
    "insertAdjacentHTML": re.compile(r"\binsertAdjacentHTML\s*\("),
    "document.write": re.compile(r"\bdocument\.write(?:ln)?\s*\("),
    "string timer": re.compile(r"\bset(?:Timeout|Interval)\(\s*['\"`]"),
}


class BuildError(RuntimeError):
    pass


def project_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def strip_dev_only(text: str, name: str) -> str:
    out, inside = [], False
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        if START in line:
            if inside:
                raise BuildError(f"{name}:{number}: nested {START}")
            inside = True
        elif END in line:
            if not inside:
                raise BuildError(f"{name}:{number}: {END} without {START}")
            inside = False
        elif not inside:
            out.append(line)
    if inside:
        raise BuildError(f"{name}: unterminated {START}")
    return "".join(out)


def check_signed(out: Path) -> None:
    problems = []
    for path in sorted(out.rglob("*.js")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for label, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    problems.append(f"{path.relative_to(out)}:{number}: {label}: {line.strip()[:100]}")
    if problems:
        raise BuildError("signed build still executes or injects code:\n  " + "\n  ".join(problems))


def build(variant: str, out: Path, version: str) -> Path:
    if variant not in ("signed", "dev"):
        raise BuildError(f"unknown variant: {variant}")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SOURCE, out, ignore=shutil.ignore_patterns(".DS_Store"))

    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    if variant == "signed":
        manifest["browser_specific_settings"]["gecko"]["update_url"] = UPDATE_URL
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for path in sorted(out.rglob("*.js")):
        name = str(path.relative_to(out))
        text = path.read_text(encoding="utf-8")
        # Validate markers in both variants so a broken region fails before release.
        stripped = strip_dev_only(text, name)
        if variant == "signed":
            text = stripped
            if name in BUILD_FILES:
                if text.count(BUILD_LINE) != 1:
                    raise BuildError(f"{name}: expected exactly one {BUILD_LINE!r}")
                text = text.replace(BUILD_LINE, "const KITSUNE_BUILD = 'signed';")
            path.write_text(text, encoding="utf-8")
    if variant == "signed":
        check_signed(out)
    return out


def write_zip(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(source).as_posix())
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("variant", choices=("signed", "dev"))
    parser.add_argument("--out", type=Path, help="output directory (default build/extension-<variant>)")
    parser.add_argument("--zip", type=Path, help="also write the built extension as this zip/xpi")
    parser.add_argument("--version", help="manifest version (default: pyproject.toml version)")
    args = parser.parse_args(argv)
    out = args.out or ROOT / "build" / f"extension-{args.variant}"
    try:
        built = build(args.variant, out, args.version or project_version())
        if args.zip:
            write_zip(built, args.zip)
    except BuildError as exc:
        print(f"build_extension: {exc}", file=sys.stderr)
        return 1
    print(built)
    return 0


if __name__ == "__main__":
    sys.exit(main())
