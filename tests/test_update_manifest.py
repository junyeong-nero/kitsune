"""Firefox update manifest for the signed XPI (scripts/update_manifest.py)."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_extension = load("build_extension")
update_manifest = load("update_manifest")


class UpdateManifestTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def xpi(self, name, version="9.9.9"):
        built = build_extension.build("signed", self.tmp / "signed", version)
        return build_extension.write_zip(built, self.tmp / name)

    def test_lists_signed_xpi_with_link_hash_and_min_version(self):
        xpi = self.xpi("kitsune-9.9.9.xpi")
        data = update_manifest.update_manifest(xpi)
        gecko = json.loads((ROOT / "extension" / "manifest.json").read_text())["browser_specific_settings"]["gecko"]
        self.assertEqual(list(data["addons"]), ["kitsune@junyeong-nero.github.io"])
        self.assertEqual(data["addons"]["kitsune@junyeong-nero.github.io"]["updates"], [{
            "version": "9.9.9",
            "update_link": "https://github.com/junyeong-nero/kitsune/releases/download/v9.9.9/kitsune-9.9.9.xpi",
            "update_hash": "sha256:" + hashlib.sha256(xpi.read_bytes()).hexdigest(),
            "applications": {"gecko": {"strict_min_version": gecko["strict_min_version"]}},
        }])

    def test_main_writes_json(self):
        xpi = self.xpi("kitsune-9.9.9.xpi")
        out = self.tmp / "updates.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(update_manifest.main([str(xpi), "--out", str(out)]), 0)
        self.assertEqual(json.loads(out.read_text()), update_manifest.update_manifest(xpi))

    def test_xpi_name_must_match_manifest_version(self):
        xpi = self.xpi("kitsune-1.0.0.xpi")
        with self.assertRaisesRegex(update_manifest.UpdateManifestError, "expected kitsune-9.9.9.xpi"):
            update_manifest.update_manifest(xpi)


if __name__ == "__main__":
    unittest.main()
