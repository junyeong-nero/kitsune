"""Track the size of the default MCP tool definitions (#88, #31)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "tool_schema_size.py"
SNAPSHOT = ROOT / "tests" / "tool_schema_sizes.json"


class ToolSchemaSizeTests(unittest.TestCase):
    def test_sizes_match_snapshot(self):
        env = {**os.environ, "KITSUNE_CAPS": "core"}
        result = subprocess.run([sys.executable, str(SCRIPT), "--json"],
                                env=env, capture_output=True, text=True, check=True)
        if json.loads(result.stdout) == json.loads(SNAPSHOT.read_text()):
            return
        report = subprocess.run([sys.executable, str(SCRIPT)],
                                env=env, capture_output=True, text=True, check=True).stdout
        self.fail("Tool definition sizes changed (every tool is resent with each model request).\n"
                  f"{report}\nIf the change is intended, run `uv run python scripts/tool_schema_size.py "
                  "--update` and commit tests/tool_schema_sizes.json.")


if __name__ == "__main__":
    unittest.main()
