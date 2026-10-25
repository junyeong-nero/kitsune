"""Published optional parameters drop the nullable anyOf and empty defaults (#142).

tools/list publishes each Kitsune-owned tool's untitled schema (#120) with every non-required
`anyOf: [X, {"type": "null"}]` parameter whose default is null rewritten to X, and
`default` removed where it is null or "". Calls are still validated by the registered
pydantic argument models, so for every tool and every sample argument set, explicit
nulls included, this checks that the tool receives the same arguments as before, and
that the published schema accepts and rejects what the untitled one does except an
explicit null for a rewritten parameter, which the tool handles as omitted.
"""
import unittest

import jsonschema
from mcp.server.fastmcp.tools import Tool
from pydantic import ValidationError

from kitsune import server, tools
from test_tool_schema_titles import arguments, listings, sample

INVALID = "invalid"


def tool_arguments(tool: Tool, case):
    """The keyword arguments FastMCP passes to the tool for `case`, or INVALID."""
    if not isinstance(case, dict):
        return INVALID
    metadata = tool.fn_metadata
    try:
        return metadata.arg_model.model_validate(metadata.pre_parse_json(case)).model_dump_one_level()
    except ValidationError:
        return INVALID


def rewritten(untitled: dict, published: dict) -> tuple[set, set]:
    """Parameters whose nullable anyOf was collapsed, and those whose default was dropped."""
    collapsed = {name for name, parameter in untitled["properties"].items()
                 if "anyOf" in parameter and "anyOf" not in published["properties"][name]}
    dropped = {name for name, parameter in untitled["properties"].items()
               if "default" in parameter and "default" not in published["properties"][name]}
    return collapsed, dropped


class ToolSchemaOptionalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        listing = listings()
        cls.published = {name: tool["inputSchema"] for name, tool in listing["published"].items()}
        cls.untitled = {name: server._untitled_schema(tool["inputSchema"])
                        for name, tool in listing["generated"].items()}
        cls.tools = {name: Tool.from_function(getattr(tools, name)) for name in cls.published}

    def test_every_tool_is_compared_against_its_registered_model(self):
        self.assertEqual(len(self.published), 76)
        for name, tool in self.tools.items():
            self.assertEqual(server._untitled_schema(tool.parameters), self.untitled[name], name)

    def test_only_nullable_optionals_and_empty_defaults_change(self):
        collapsed_total = dropped_total = 0
        for name, untitled in self.untitled.items():
            with self.subTest(tool=name):
                published = self.published[name]
                self.assertEqual({**published, "properties": None}, {**untitled, "properties": None})
                self.assertEqual(set(published["properties"]), set(untitled["properties"]))
                collapsed, dropped = rewritten(untitled, published)
                self.assertLessEqual(collapsed, dropped)
                self.assertFalse(dropped & set(untitled.get("required", ())))
                for parameter, before in untitled["properties"].items():
                    after = published["properties"][parameter]
                    if parameter in collapsed:
                        (variant,) = [v for v in before["anyOf"] if v != {"type": "null"}]
                        self.assertEqual(len(before["anyOf"]), 2, parameter)
                        self.assertIsNone(before["default"], parameter)
                        before = {**variant, **{k: v for k, v in before.items() if k != "anyOf"}}
                    if parameter in dropped:
                        self.assertIn(before["default"], (None, ""), parameter)
                        before = {k: v for k, v in before.items() if k != "default"}
                    self.assertEqual(after, before, parameter)
                    if {"type": "null"} in after.get("anyOf", []):  # null differs from omitting it
                        self.assertIsNotNone(after.get("default"), parameter)
                    self.assertFalse("default" in after and after["default"] in (None, ""), parameter)
                collapsed_total += len(collapsed)
                dropped_total += len(dropped)
        self.assertGreater(collapsed_total, 100)
        self.assertGreater(dropped_total, collapsed_total)

    def test_null_with_a_different_default_stays_published(self):
        # Kitsune's browser_handle_dialog: accept=None clears the dialog policy; omitting it
        # means accept=True. (The published core tool is Playwright's, with no such parameter.)
        accept = server._published_schema(Tool.from_function(tools.browser_handle_dialog).parameters)
        accept = accept["properties"]["accept"]
        self.assertEqual(accept["anyOf"], [{"type": "boolean"}, {"type": "null"}])
        self.assertIs(accept["default"], True)

    def test_omitting_a_dropped_default_passes_the_same_arguments(self):
        for name, untitled in self.untitled.items():
            _, dropped = rewritten(untitled, self.published[name])
            minimal = {p: sample(untitled["properties"][p]) for p in untitled.get("required", [])}
            omitted = tool_arguments(self.tools[name], minimal)
            self.assertNotEqual(omitted, INVALID, name)
            for parameter in dropped:
                default = untitled["properties"][parameter]["default"]
                explicit = tool_arguments(self.tools[name], {**minimal, parameter: default})
                self.assertEqual(explicit, omitted, (name, parameter))

    def test_sample_inputs_are_handled_the_same(self):
        for name, untitled in self.untitled.items():
            with self.subTest(tool=name):
                published, tool = self.published[name], self.tools[name]
                jsonschema.Draft202012Validator.check_schema(published)
                old = jsonschema.Draft202012Validator(untitled)
                new = jsonschema.Draft202012Validator(published)
                collapsed, _ = rewritten(untitled, published)
                minimal = {p: sample(untitled["properties"][p]) for p in untitled.get("required", [])}
                complete = {p: sample(sub) for p, sub in untitled["properties"].items()}
                cases = arguments(untitled) + [{**minimal, **dict.fromkeys(collapsed)},
                                               {**complete, **dict.fromkeys(collapsed)}]
                explicit_nulls = 0
                for case in cases:
                    nulls = {p for p in collapsed if isinstance(case, dict) and p in case and case[p] is None}
                    if not nulls:
                        self.assertEqual(new.is_valid(case), old.is_valid(case), case)
                        continue
                    # An explicit null is no longer advertised, but the tool still accepts
                    # it and receives exactly what omitting the parameter gives.
                    explicit_nulls += 1
                    omitted = {p: value for p, value in case.items() if p not in nulls}
                    self.assertFalse(new.is_valid(case), case)
                    self.assertEqual(new.is_valid(omitted), old.is_valid(case), case)
                    self.assertEqual(tool_arguments(tool, case), tool_arguments(tool, omitted), case)
                    if old.is_valid(case):
                        self.assertNotEqual(tool_arguments(tool, case), INVALID, case)
                self.assertEqual(explicit_nulls > 0, bool(collapsed))


if __name__ == "__main__":
    unittest.main()
