"""Build the Kitsune browser extension variants.

    python scripts/build_extension.py signed --out build/extension-signed
    python scripts/build_extension.py dev --out build/extension-dev --zip dist/kitsune-extension-dev.zip

signed: the AMO (Mozilla-signed) build. Every `// @kitsune-dev-only:start` ...
        `// @kitsune-dev-only:end` region is removed and KITSUNE_BUILD becomes
        'signed', because Mozilla's add-on policy forbids executing code that is
        not part of the package. The build fails if code execution remains.
        It also gets gecko.update_url so installed copies update from releases.
dev:    the full extension (browser_js etc.), loaded as a temporary add-on.
chrome: a Chrome MV3 build (#589), loaded unpacked. Firefox's manifest is translated to MV3 and
        extension/chrome/ maps the `browser` API onto `chrome`. Dev-only regions are removed as in
        the signed build, since MV3 forbids executing code that is not part of the package.
        KITSUNE_BUILD becomes 'chrome'. The Firefox builds leave extension/chrome/ out.

All stamp manifest.json with the pyproject version so extension and server match.
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
VARIANTS = ("signed", "dev", "chrome")
CHROME_DIR = "chrome"
CHROME_COMPAT = f"{CHROME_DIR}/compat.js"
# MV3 keeps host permissions apart. Chrome has no theme API, and only policy-installed extensions
# may block in webRequest.
CHROME_HOST_PERMISSIONS = ("<all_urls>",)
CHROME_DROPPED_PERMISSIONS = ("webRequestBlocking", "theme")
# The extension pages (one directory deep), which load compat.js before their own scripts.
CHROME_PAGES = ("popup/popup.html", "options/options.html")
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


def chrome_manifest(manifest: dict) -> dict:
    """Translate the Firefox MV2 manifest to Chrome MV3."""
    manifest = {key: value for key, value in manifest.items() if key != "browser_specific_settings"}
    permissions = manifest["permissions"]
    manifest["manifest_version"] = 3
    # Service workers exchange WebSocket messages without being suspended since Chrome 116.
    manifest["minimum_chrome_version"] = "116"
    manifest["permissions"] = [p for p in permissions
                               if p not in CHROME_HOST_PERMISSIONS + CHROME_DROPPED_PERMISSIONS] + ["scripting"]
    manifest["host_permissions"] = [p for p in permissions if p in CHROME_HOST_PERMISSIONS]
    manifest["action"] = manifest.pop("browser_action")
    manifest["background"] = {"service_worker": f"{CHROME_DIR}/service-worker.js"}
    manifest["content_scripts"] = [{**entry, "js": [CHROME_COMPAT, *entry["js"]]}
                                   for entry in manifest["content_scripts"]]
    return manifest


def add_chrome_compat(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    first = html.find("<script ")
    if first < 0:
        raise BuildError(f"{page.name}: no <script> to load {CHROME_COMPAT} before")
    tag = f'<script src="../{CHROME_COMPAT}"></script>\n  '
    page.write_text(html[:first] + tag + html[first:], encoding="utf-8")


def build(variant: str, out: Path, version: str) -> Path:
    if variant not in VARIANTS:
        raise BuildError(f"unknown variant: {variant}")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SOURCE, out, ignore=shutil.ignore_patterns(".DS_Store"))
    if variant != "chrome":
        shutil.rmtree(out / CHROME_DIR)

    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    if variant == "signed":
        manifest["browser_specific_settings"]["gecko"]["update_url"] = UPDATE_URL
    if variant == "chrome":
        manifest = chrome_manifest(manifest)
        for page in CHROME_PAGES:
            add_chrome_compat(out / page)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for path in sorted(out.rglob("*.js")):
        name = path.relative_to(out).as_posix()  # BUILD_FILES spelling, also on Windows
        text = path.read_text(encoding="utf-8")
        # Validate markers in both variants so a broken region fails before release.
        stripped = strip_dev_only(text, name)
        if variant != "dev":
            text = stripped
            if name in BUILD_FILES:
                if text.count(BUILD_LINE) != 1:
                    raise BuildError(f"{name}: expected exactly one {BUILD_LINE!r}")
                text = text.replace(BUILD_LINE, f"const KITSUNE_BUILD = '{variant}';")
            path.write_text(text, encoding="utf-8")
    if variant != "dev":
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
    parser.add_argument("variant", choices=VARIANTS)
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
