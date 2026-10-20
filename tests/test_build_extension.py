"""Extension build variants (scripts/build_extension.py)."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_extension", ROOT / "scripts" / "build_extension.py")
build_extension = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_extension)


class BuildExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, variant, version="9.9.9"):
        return build_extension.build(variant, Path(self.tmp.name) / variant, version)

    def test_signed_build_has_no_code_execution(self):
        out = self.build("signed")
        content = (out / "content" / "content.js").read_text()
        self.assertNotIn("eval(", content)
        self.assertNotIn(build_extension.START, content)
        self.assertIn("const KITSUNE_BUILD = 'signed';", content)
        self.assertIn("const KITSUNE_BUILD = 'signed';", (out / "background" / "background.js").read_text())
        self.assertEqual(json.loads((out / "manifest.json").read_text())["version"], "9.9.9")

    def test_dev_build_keeps_javascript_tools(self):
        out = self.build("dev")
        content = (out / "content" / "content.js").read_text()
        self.assertEqual(content.count("eval("), 2)
        self.assertIn("const KITSUNE_BUILD = 'dev';", content)

    def test_only_signed_build_has_update_url(self):
        signed = json.loads((self.build("signed") / "manifest.json").read_text())
        self.assertEqual(signed["browser_specific_settings"]["gecko"]["update_url"], build_extension.UPDATE_URL)
        self.assertEqual(signed["browser_specific_settings"]["gecko"]["id"], "kitsune@junyeong-nero.github.io")
        for manifest in (
            json.loads((self.build("dev") / "manifest.json").read_text()),
            json.loads((ROOT / "extension" / "manifest.json").read_text()),
        ):
            self.assertNotIn("update_url", manifest["browser_specific_settings"]["gecko"])

    def test_markers_must_balance(self):
        for text, message in (
            ("// @kitsune-dev-only:start\nx\n", "unterminated"),
            ("x\n// @kitsune-dev-only:end\n", "without"),
            ("// @kitsune-dev-only:start\n// @kitsune-dev-only:start\n", "nested"),
        ):
            with self.subTest(message), self.assertRaisesRegex(build_extension.BuildError, message):
                build_extension.strip_dev_only(text, "x.js")

    def test_forbidden_patterns_fail_the_signed_build(self):
        out = Path(self.tmp.name) / "bad"
        out.mkdir()
        (out / "a.js").write_text("el.innerHTML = html;\nsetTimeout('x()', 1);\n")
        with self.assertRaisesRegex(build_extension.BuildError, "innerHTML.*\n.*string timer"):
            build_extension.check_signed(out)

    def test_repository_manifest_matches_project_version(self):
        manifest = json.loads((ROOT / "extension" / "manifest.json").read_text())
        self.assertEqual(manifest["version"], build_extension.project_version())


if __name__ == "__main__":
    unittest.main()
