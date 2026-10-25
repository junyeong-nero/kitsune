"""The two PyPI distributions: kitsune-browser has the code, kitsune-browser-mcp installs its MCP server."""
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from kitsune import version

ROOT = Path(__file__).resolve().parents[1]


def project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.core_toml = project(ROOT / "pyproject.toml")
        self.core = self.core_toml["project"]
        self.mcp = project(ROOT / "packages" / "kitsune-browser-mcp" / "pyproject.toml")["project"]

    def test_the_mcp_package_pins_kitsune_browser_exactly(self):
        # The MCP server only uses a bridge of its own build, and both come from kitsune-browser.
        self.assertEqual((self.core["name"], self.mcp["name"]), ("kitsune-browser", "kitsune-browser-mcp"))
        self.assertEqual(self.mcp["version"], self.core["version"])
        self.assertEqual(self.mcp["dependencies"], [f"kitsune-browser[mcp]=={self.core['version']}"])
        self.assertEqual(self.mcp["optional-dependencies"],
                         {"macos": [f"kitsune-browser[macos]=={self.core['version']}"]})

    def test_commands(self):
        self.assertEqual(self.mcp["scripts"], {"kitsune": "kitsune.server:main", "kitsune-browser-mcp": "kitsune.server:main",
                                               "kitsune-bridge": "kitsune.bridge:run"})
        self.assertEqual(self.core["scripts"], {"kitsune-bridge": "kitsune.bridge:run"})

    def test_kitsune_browser_needs_no_mcp(self):
        names = {dependency.split("[")[0].split(">")[0].split("=")[0].split("<")[0] for dependency in self.core["dependencies"]}
        self.assertEqual(names, {"httpx", "pydantic", "websockets"}, "pydantic: kitsune.tools' parameter descriptions")
        self.assertEqual(self.core["optional-dependencies"]["mcp"], ["mcp[cli]>=1.2.0,<2"])
        self.assertEqual(self.core_toml["tool"]["uv"]["workspace"]["members"], ["packages/kitsune-browser-mcp"])

    def test_the_bridge_build_names_kitsune_browser(self):
        # The distribution with the code: installed alone, kitsune-browser-mcp is absent.
        with patch.object(version, "version", return_value="9.9.9") as installed:
            self.assertEqual(version.package_version(), "9.9.9")
        installed.assert_called_once_with("kitsune-browser")


if __name__ == "__main__":
    unittest.main()
