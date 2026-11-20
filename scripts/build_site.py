"""Build the GitHub Pages site: the landing page at the root and the MkDocs docs under docs/.

The docs used to be served at the root, so every docs page also gets a redirect page at its old
path (e.g. installation/ -> docs/installation/), keeping the #fragment.

    uv run --only-group docs python scripts/build_site.py [OUT_DIR]   # default: _site
    python -m http.server -d _site                                   # preview
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REDIRECT = """<!doctype html>
<meta charset="utf-8">
<title>Kitsune docs moved</title>
<link rel="canonical" href="{url}">
<meta http-equiv="refresh" content="0; url={url}">
<script>location.replace({url_js} + location.search + location.hash)</script>
<p>This page moved to <a href="{url}">{url}</a>.</p>
"""


def main() -> None:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "_site"
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(ROOT / "landing", out)

    docs = out / "docs"
    subprocess.run([sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(docs)], cwd=ROOT, check=True)

    for page in sorted(docs.rglob("index.html")):
        old = page.parent.relative_to(docs)
        if old == Path("."):
            continue
        stub = out / old / "index.html"
        if stub.exists():
            sys.exit(f"landing/{old.as_posix()} collides with the docs page of the same name")
        url = "../" * len(old.parts) + f"docs/{old.as_posix()}/"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(REDIRECT.format(url=url, url_js=json.dumps(url)))


if __name__ == "__main__":
    main()
