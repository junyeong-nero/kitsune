"""Write the Firefox update manifest (updates.json) for a signed Kitsune XPI.

    python scripts/update_manifest.py kitsune-<version>.xpi --out updates.json

The signed build's gecko.update_url points at the latest GitHub release's
updates.json, so the release that attaches this file is the one installed
copies update to. Only the XPI's own version is listed. Format:
https://extensionworkshop.com/documentation/manage/updating-your-extension/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

RELEASE_DOWNLOAD = "https://github.com/junyeong-nero/kitsune/releases/download"


class UpdateManifestError(RuntimeError):
    pass


def update_manifest(xpi: Path) -> dict:
    with zipfile.ZipFile(xpi) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    gecko = manifest["browser_specific_settings"]["gecko"]
    version = manifest["version"]
    # The link is built from the version, so the asset must carry the matching name.
    if xpi.name != f"kitsune-{version}.xpi":
        raise UpdateManifestError(f"{xpi.name}: expected kitsune-{version}.xpi for manifest version {version}")
    update = {
        "version": version,
        "update_link": f"{RELEASE_DOWNLOAD}/v{version}/{xpi.name}",
        "update_hash": "sha256:" + hashlib.sha256(xpi.read_bytes()).hexdigest(),
        # Firefox reads update compatibility from "applications", not browser_specific_settings.
        "applications": {"gecko": {"strict_min_version": gecko["strict_min_version"]}},
    }
    return {"addons": {gecko["id"]: {"updates": [update]}}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("xpi", type=Path, help="signed kitsune-<version>.xpi")
    parser.add_argument("--out", type=Path, default=Path("updates.json"), help="output file (default updates.json)")
    args = parser.parse_args(argv)
    try:
        data = update_manifest(args.xpi)
    except UpdateManifestError as exc:
        print(f"update_manifest: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
