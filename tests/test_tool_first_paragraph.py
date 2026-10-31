"""Tool definitions keep the action contract when a client sends only first paragraphs (#135).

Some clients (Claude Code --bare) cut each tool description at its first blank line but
send the input schema in full. Parameters are therefore documented as schema descriptions,
and each first paragraph says what the tool does and what a caller must know. The shared
action parameters of the Kitsune-owned tools are documented once, in browser_check's schema,
and the other tools point there instead of repeating them (#88). The core tools publish
Playwright MCP's descriptions verbatim and are not checked here.
"""
import importlib.util
from pathlib import Path
import unittest

from test_tool_schema_titles import listings

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("tool_schema_size", ROOT / "scripts" / "tool_schema_size.py")
tool_schema_size = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool_schema_size)

# Parameters the action contract relies on; browser_check documents each of them.
CONTRACT_PARAMETERS = ("tab_id", "ref", "element", "wait", "expect", "confirm_token", "first")
EXPECT_GRAMMAR = ('"url"', '"url_changed"', '"new_tab"', '"selector"', '"gone"', '"value"',
                  '"value_changed"', '"text"', '"text_gone"', '"timeout"', "outcome_not_observed",
                  "already ran")


def properties(definition: dict) -> dict:
    return definition["inputSchema"].get("properties", {})


class FirstParagraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full = listings("all,storage,history")["published"]
        cls.first = {name: tool_schema_size.first_paragraph(definition["description"])
                     for name, definition in cls.full.items()}

    def test_every_tool_is_checked(self):
        self.assertEqual(len(self.full), 83)

    def test_check_documents_the_shared_contract_in_its_schema(self):
        click = properties(self.full["browser_check"])
        for parameter in CONTRACT_PARAMETERS:
            with self.subTest(parameter=parameter):
                self.assertTrue(click[parameter].get("description", "").strip(), "no schema description")
        for keyword in EXPECT_GRAMMAR:
            self.assertIn(keyword, click["expect"]["description"])
        self.assertIn("ambiguous_target", click["first"]["description"])
        self.assertIn("needs_user", click["confirm_token"]["description"])
        for word in ("ok=true", "expect", "opened_tabs"):
            self.assertIn(word, self.first["browser_check"])

    def test_contract_is_documented_once(self):
        # #88: the grammar and the shared parameter texts are not repeated in other tools.
        for name, definition in self.full.items():
            if name == "browser_check":
                continue
            with self.subTest(tool=name):
                for parameter in ("expect", "element", "first"):
                    self.assertNotIn("description", properties(definition).get(parameter, {}))
                self.assertNotIn("outcome_not_observed", str(definition))

    def test_action_first_paragraphs_say_what_ok_means_and_point_to_check(self):
        actions = [name for name, definition in self.full.items()
                   if "expect" in properties(definition) and name != "browser_check"]
        self.assertGreater(len(actions), 3)
        for name in actions:
            with self.subTest(tool=name):
                first = self.first[name]
                self.assertRegex(first, r"\bok(=true)? means (dispatched|handled)\b")
                self.assertRegex(first, r"\bexpect\b")
                self.assertIn("browser_check", first)

    def test_tools_taking_a_target_point_to_check(self):
        targets = [name for name, definition in self.full.items()
                   if "element" in properties(definition) and name != "browser_check"]
        self.assertGreater(len(targets), 8)
        for name in targets:
            with self.subTest(tool=name):
                self.assertIn("browser_check", self.first[name])

    def test_tab_id_is_described_wherever_it_is_accepted(self):
        tools = [name for name, definition in self.full.items() if "tab_id" in properties(definition)]
        self.assertGreater(len(tools), 30)
        for name in tools:
            with self.subTest(tool=name):
                self.assertTrue(properties(self.full[name])["tab_id"].get("description", "").strip())

    def test_parameters_are_documented_in_the_schema_not_args_blocks(self):
        for name, definition in self.full.items():
            with self.subTest(tool=name):
                self.assertNotIn("Args:", definition["description"])
                self.assertTrue(self.first[name].strip())

    def test_first_paragraph_is_cut_at_the_first_blank_line(self):
        self.assertEqual(tool_schema_size.first_paragraph("One\nline.\n\nTwo.\n\nThree."), "One\nline.")
        self.assertEqual(tool_schema_size.first_paragraph("One.\n  \nTwo."), "One.")
        self.assertEqual(tool_schema_size.first_paragraph("Only."), "Only.")


if __name__ == "__main__":
    unittest.main()
