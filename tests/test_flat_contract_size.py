"""Reproducible design sizes, not token savings or benchmark evidence."""
import json
import unittest

from scripts.flat_contract import DESIGN
from scripts.flat_contract_size import compact, definition_size, measure, text_size


class FlatContractSizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = measure()

    def test_characters_bytes_and_array_framing_are_separate(self):
        self.assertEqual(text_size("가"), {"chars": 1, "utf8_bytes": 3})
        tool = {"name": "example", "description": "가", "inputSchema": {"type": "object"}}
        size = definition_size([tool])
        self.assertEqual(size["sum_chars"], len(compact(tool)))
        self.assertEqual(size["sum_utf8_bytes"], size["sum_chars"] + 2)
        self.assertEqual(size["array_chars"], size["sum_chars"] + 2)

    def test_measured_rows_and_input_hashes_match_the_reviewed_snapshot(self):
        snapshot = json.loads((DESIGN / "499-interface-costs.json").read_text())
        rows = {}
        for name, row in self.report["rows"].items():
            rows[name] = {"count": row["definitions"]["count"], "definitions": row["definitions"]["sum_chars"],
                          "name_description_inputSchema": row["definitions"]["name_description_input_schema_sum_chars"],
                          "instructions": row.get("instructions", row.get("common_guide"))["chars"],
                          "sdk": row.get("sdk_guide", {"chars": 0})["chars"], "sum": row["initial_component_sum_chars"]}
        self.assertEqual(rows, snapshot["rows"])
        self.assertEqual(self.report["sha256"], snapshot["sha256"])

    def test_scripting_guide_and_runner_cost_are_not_hidden(self):
        rows = self.report["rows"]
        for mode in ("act", "read", "draft"):
            c, d = rows[f"flat-C-{mode}"], rows[f"flat-D-{mode}"]
            self.assertEqual(d["initial_component_sum_chars"] - c["initial_component_sum_chars"],
                             d["definitions"]["sum_chars"] - c["definitions"]["sum_chars"] + d["sdk_guide"]["chars"])
            self.assertGreater(d["initial_component_sum_chars"], c["initial_component_sum_chars"])
        gateway = rows["legacy-gateway-only-act"]
        self.assertGreater(gateway["authorized_definitions_not_initial_payload"]["sum_chars"],
                           gateway["definitions"]["sum_chars"])
        self.assertEqual(sum(case["programs"] for case in self.report["example_code_not_model_generated"].values()), 7)


if __name__ == "__main__":
    unittest.main()
