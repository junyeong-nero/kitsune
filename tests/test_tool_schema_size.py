"""Track the size of the core MCP tool definitions, full and first-paragraph views (#88, #31, #135),
and of the server instructions (#386)."""
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

    def test_core_still_has_27_tools(self):
        self.assertEqual(len(measured_sizes()), 27)

    def test_sizes_within_budget(self):
        problems = tool_schema_size.over_budget(measured_sizes())
        self.assertEqual(problems, [], "Tool definitions grew past their budget (#88). Trim them, or "
                         "raise TOTAL_BUDGETS/FIRST_PARAGRAPH_BUDGETS/TOOL_BUDGET in scripts/tool_schema_size.py "
                         "deliberately.")

    def test_budget_catches_growth(self):
        sizes = json.loads(SNAPSHOT.read_text())

        def grow_click(field, size):  # browser_click counts towards every budgeted total
            return tool_schema_size.over_budget({**sizes, "browser_click": {**sizes["browser_click"],
                                                                            field: size}})

        budget = tool_schema_size.TOOL_BUDGET
        self.assertIn(f"browser_click: {budget + 1} > per-tool budget {budget}", grow_click("total", budget + 1))
        for view, (field, budgets) in tool_schema_size.VIEWS.items():
            for label, total_budget in budgets.items():
                self.assertTrue(any(problem.startswith(f"{label} ({view}): ")
                                    for problem in grow_click(field, total_budget)), (view, label))
        self.assertEqual(set(tool_schema_size.FIRST_PARAGRAPH_BUDGETS), set(tool_schema_size.TOTAL_BUDGETS))

    def test_instructions_within_budget(self):
        sizes = tool_schema_size.measure_instructions()
        self.assertEqual(set(sizes), {"act", "read", "draft"})
        self.assertEqual(tool_schema_size.instructions_over_budget(sizes), [],
                         "The server instructions grew past their budget (#386); they are resent with every model "
                         "request. Trim them, or raise INSTRUCTIONS_BUDGETS in scripts/tool_schema_size.py "
                         "deliberately.")

    def test_act_instruction_budget_does_not_exceed_v071(self):
        self.assertLessEqual(tool_schema_size.INSTRUCTIONS_BUDGETS["act"], 747)

    def test_instructions_budget_catches_growth(self):
        budgets = tool_schema_size.INSTRUCTIONS_BUDGETS
        self.assertEqual(tool_schema_size.instructions_over_budget(budgets), [])
        self.assertEqual(tool_schema_size.instructions_over_budget({**budgets, "act": budgets["act"] + 1}),
                         [f"instructions (act): {budgets['act'] + 1} > budget {budgets['act']}"])


if __name__ == "__main__":
    unittest.main()
