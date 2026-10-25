import json
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP_PACKAGE = ROOT / "packages" / "kitsune-browser-mcp"


class ServerJsonTests(unittest.TestCase):
    """server.json is published to the MCP Registry on each release tag."""

    def setUp(self):
        self.server = json.loads((ROOT / "server.json").read_text())
        # The registry runs the MCP server's package, kitsune-browser-mcp; its version is the project's.
        self.project = tomllib.loads((MCP_PACKAGE / "pyproject.toml").read_text())["project"]
        self.core = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    def test_versions_match_pyproject(self):
        self.assertEqual(self.server["version"], self.core["version"])
        self.assertEqual([p["version"] for p in self.server["packages"]], [self.project["version"]])

    def test_package_is_this_project(self):
        (package,) = self.server["packages"]
        self.assertEqual(package["registryType"], "pypi")
        self.assertEqual(package["identifier"], self.project["name"])
        # Registry clients run `uvx <identifier>`, which needs a script of that name.
        self.assertIn(self.project["name"], self.project["scripts"])

    def test_readme_declares_server_name(self):
        # The registry verifies PyPI ownership by finding this line in the package's README.
        self.assertIn(f"mcp-name: {self.server['name']}", (MCP_PACKAGE / self.project["readme"]).read_text())

    def test_description_fits_registry_limit(self):
        self.assertLessEqual(len(self.server["description"]), 100)


if __name__ == "__main__":
    unittest.main()
