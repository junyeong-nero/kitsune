"""Track the size of the default MCP tool definitions (#88, #31)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "tool_schema_size.py"
SNAPSHOT = ROOT / "tests" / "tool_schema_sizes.json"

_spec = importlib.util.spec_from_file_location("tool_schema_size", SCRIPT)
tool_schema_size = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool_schema_size)


def run_script(*args) -> str:
    env = {**os.environ, "KITSUNE_CAPS": "core"}
    env.pop("KITSUNE_TOOL_PRESET", None)
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          env=env, capture_output=True, text=True, check=True).stdout


def measured_sizes() -> dict:
    return json.loads(run_script("--json"))


class ToolSchemaSizeTests(unittest.TestCase):
    def test_sizes_match_snapshot(self):
        if measured_sizes() == json.loads(SNAPSHOT.read_text()):
            return
        report = run_script()
        self.fail("Tool definition sizes changed (every tool is resent with each model request).\n"
                  f"{report}\nIf the change is intended, run `uv run python scripts/tool_schema_size.py "
                  "--update` and commit tests/tool_schema_sizes.json.")

    def test_sizes_within_budget(self):
        problems = tool_schema_size.over_budget(measured_sizes())
        self.assertEqual(problems, [], "Tool definitions grew past their budget (#88). Trim them, or "
                         "raise TOTAL_BUDGETS/TOOL_BUDGET in scripts/tool_schema_size.py deliberately.")

    def test_budget_catches_growth(self):
        sizes = json.loads(SNAPSHOT.read_text())

        def grow_click(total):  # browser_click counts towards every budgeted total
            return tool_schema_size.over_budget({**sizes, "browser_click": {**sizes["browser_click"],
                                                                            "total": total}})

        budget = tool_schema_size.TOOL_BUDGET
        self.assertIn(f"browser_click: {budget + 1} > per-tool budget {budget}", grow_click(budget + 1))
        for label, total_budget in tool_schema_size.TOTAL_BUDGETS.items():
            self.assertTrue(any(problem.startswith(f"{label}: ") for problem in grow_click(total_budget)),
                            label)


if __name__ == "__main__":
    unittest.main()
