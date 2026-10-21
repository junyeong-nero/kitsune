"""Tool definitions keep the action contract when a client sends only first paragraphs (#135).

Some clients (Claude Code --bare) cut each tool description at its first blank line but
send the input schema in full. The contract therefore lives in parameter descriptions,
and each first paragraph says what the tool does and what a caller must know.
"""
import importlib.util
from pathlib import Path
import unittest

from test_tool_schema_titles import listings

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("tool_schema_size", ROOT / "scripts" / "tool_schema_size.py")
tool_schema_size = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool_schema_size)

# Parameters the action contract relies on, wherever a tool accepts them.
CONTRACT_PARAMETERS = ("expect", "wait", "tab_id", "ref", "confirm_token", "first")
EXPECT_GRAMMAR = ('"url"', '"url_changed"', '"new_tab"', '"selector"', '"gone"', '"value"',
                  '"value_changed"', '"text"', '"text_gone"', '"timeout"', "outcome_not_observed")


def first_paragraph_view(definition: dict) -> dict:
    return {**definition, "description": tool_schema_size.first_paragraph(definition.get("description", ""))}


class FirstParagraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full = listings()["published"]
        cls.cut = {name: first_paragraph_view(definition) for name, definition in cls.full.items()}

    def test_contract_parameters_are_described_in_the_first_paragraph_view(self):
        checked = 0
        for name, definition in self.full.items():
            parameters = [p for p in CONTRACT_PARAMETERS if p in definition["inputSchema"]["properties"]]
            if name == "browser_snapshot":
                parameters.append("text")
            for parameter in parameters:
                with self.subTest(tool=name, parameter=parameter):
                    schema = self.cut[name]["inputSchema"]["properties"].get(parameter)
                    self.assertIsNotNone(schema, "absent from the first-paragraph view")
                    self.assertTrue(schema.get("description", "").strip(), "no schema description")
                    checked += 1
        self.assertGreater(checked, 100)

    def test_action_first_paragraphs_say_what_ok_means_and_name_expect_and_tab_id(self):
        actions = [name for name, definition in self.full.items() if "expect" in definition["inputSchema"]["properties"]]
        self.assertGreater(len(actions), 10)
        for name in actions:
            with self.subTest(tool=name):
                first = self.cut[name]["description"]
                for word in ("expect", "tab_id"):
                    self.assertRegex(first, rf"\b{word}\b")
                self.assertRegex(first, r"\bok(=true)? (only )?means (dispatched|handled)\b")

    def test_click_carries_the_shared_contract(self):
        click = self.cut["browser_click"]
        self.assertIn("opened_tabs", click["description"])
        expect = click["inputSchema"]["properties"]["expect"]["description"]
        for keyword in EXPECT_GRAMMAR:
            self.assertIn(keyword, expect)
        for name, definition in self.full.items():
            expect = definition["inputSchema"]["properties"].get("expect")
            if expect is not None and name != "browser_click":
                self.assertIn("browser_click", expect["description"], name)

    def test_snapshot_first_paragraph_mentions_text(self):
        self.assertRegex(self.cut["browser_snapshot"]["description"], r"\btext=True\b")

    def test_parameters_are_documented_in_the_schema_not_args_blocks(self):
        for name, definition in self.full.items():
            with self.subTest(tool=name):
                self.assertNotIn("Args:", definition["description"])
                self.assertTrue(self.cut[name]["description"].strip())

    def test_first_paragraph_is_cut_at_the_first_blank_line(self):
        self.assertEqual(tool_schema_size.first_paragraph("One\nline.\n\nTwo.\n\nThree."), "One\nline.")
        self.assertEqual(tool_schema_size.first_paragraph("One.\n  \nTwo."), "One.")
        self.assertEqual(tool_schema_size.first_paragraph("Only."), "Only.")


if __name__ == "__main__":
    unittest.main()
