"""Published input schemas drop generated `title` annotations (#120).

Every Kitsune-owned tool in every capability group is compared with FastMCP's own listing:
the untitled stage removes only `title` keywords, and both schemas must accept and reject
the same sample arguments. The core tools publish Playwright MCP's schemas verbatim
instead (tests/test_playwright_tools.py). tests/test_tool_schema_optionals.py covers the optional-parameter
rewrite applied after it (#142).
"""
import inspect
import json
import os
import subprocess
import sys
import unittest

import jsonschema

from kitsune import playwright_tools, server

# FastMCP.list_tools is the unstripped listing; mcp.list_tools is what tools/list publishes.
DUMP = """
import asyncio, json
from mcp.server.fastmcp import FastMCP
from kitsune.server import mcp

def dump(tools):
    return {tool.name: tool.model_dump(mode="json", exclude_none=True) for tool in tools}

print(json.dumps({"generated": dump(asyncio.run(FastMCP.list_tools(mcp))),
                  "published": dump(asyncio.run(mcp.list_tools()))}))
"""

# Argument values of every JSON type, plus an enum miss; each is tried for each parameter.
PROBES = ["x", "not-an-enum-value", "", 0, 1, -1, 1.5, True, False, None,
          {}, {"a": 1}, [], ["x"], [1], [{"selector": "#a", "value": "b"}]]


def listings(caps="all,storage") -> dict:
    """Listings of the Kitsune-owned tools (not the Playwright core tools)."""
    env = {**os.environ, "KITSUNE_CAPS": caps}
    result = subprocess.run([sys.executable, "-c", DUMP], env=env,
                            capture_output=True, text=True, check=True)
    return {stage: {name: tool for name, tool in tools.items() if name not in playwright_tools.SCHEMAS}
            for stage, tools in json.loads(result.stdout).items()}


def removed_paths(before, after, path=()) -> list[tuple]:
    """Paths of keys present in `before` but not in `after`; anything else must be equal."""
    if isinstance(before, dict) and isinstance(after, dict):
        if set(after) - set(before):
            raise AssertionError(f"keys added at {path}: {sorted(set(after) - set(before))}")
        removed = [path + (key,) for key in before if key not in after]
        for key in after:
            removed += removed_paths(before[key], after[key], path + (key,))
        return removed
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [p for i, (b, a) in enumerate(zip(before, after)) for p in removed_paths(b, a, path + (i,))]
    if before != after:
        raise AssertionError(f"value changed at {path}: {before!r} -> {after!r}")
    return []


def names_parameter(path: tuple) -> bool:
    """Whether the last key of a schema path is a parameter name rather than a keyword."""
    is_name = after_properties = False
    for key in path:
        is_name = after_properties
        after_properties = key == "properties" and not is_name
    return is_name


def title_keywords(value, path=()):
    """Paths of `title` keys that are not parameter names."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "title" and not names_parameter(path + (key,)):
                yield path + (key,)
            yield from title_keywords(item, path + (key,))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from title_keywords(item, path + (i,))


def sample(schema):
    """A value the schema accepts: a default, the first enum member, or one of the first type."""
    if "default" in schema:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return sample(schema["anyOf"][0])
    return {"string": "x", "integer": 1, "number": 1.5, "boolean": True, "object": {},
            "array": [], "null": None}[schema["type"]]


def arguments(schema) -> list:
    """Sample tool arguments: required-only, complete, each parameter probed, and malformed."""
    properties = schema.get("properties", {})
    minimal = {name: sample(properties[name]) for name in schema.get("required", [])}
    cases = [{}, minimal, {name: sample(sub) for name, sub in properties.items()},
             {**minimal, "unexpected": 1}, [], "x", None]
    cases += [{key: value for key, value in minimal.items() if key != name} for name in minimal]
    cases += [{**minimal, name: probe} for name in properties for probe in PROBES]
    cases += [{**minimal, name: member} for name, sub in properties.items() for member in sub.get("enum", [])]
    return cases


class ToolSchemaTitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        listing = listings()
        cls.generated, cls.published = listing["generated"], listing["published"]

    def test_every_tool_is_compared(self):
        # `all` (93) plus the named-only storage group (8).
        self.assertEqual(len(self.published), 101 - len(playwright_tools.SCHEMAS))
        self.assertEqual(set(self.published), set(self.generated))

    def test_only_title_keywords_are_removed(self):
        removed_total = 0
        for name, generated in self.generated.items():
            with self.subTest(tool=name):
                # Descriptions are cleandoc-normalized on purpose; see the test below.
                generated = {**generated, "description": inspect.cleandoc(generated["description"])}
                untitled = {**self.published[name], "inputSchema": server._untitled_schema(generated["inputSchema"])}
                removed = removed_paths(generated, untitled)
                self.assertTrue(removed, "generated schema had no title to remove")
                for path in removed:
                    self.assertEqual(path[:1], ("inputSchema",), path)
                    self.assertEqual(path[-1], "title", path)
                    self.assertFalse(names_parameter(path), f"parameter removed: {path}")
                removed_total += len(removed)
                self.assertEqual(list(title_keywords(self.published[name]["inputSchema"])), [])
                self.assertEqual(self.published[name]["inputSchema"],
                                 server._published_schema(generated["inputSchema"]))
        self.assertGreater(removed_total, len(self.generated))

    def test_descriptions_are_cleandoc_normalized(self):
        # Only Python 3.13+ strips docstring indentation itself; published text must not
        # depend on the interpreter (#130).
        for name, generated in self.generated.items():
            with self.subTest(tool=name):
                published = self.published[name]["description"]
                self.assertEqual(published, inspect.cleandoc(generated["description"]))
                lines = published.splitlines()[1:]
                self.assertFalse(lines and all(not l or l[:1].isspace() for l in lines),
                                 "description keeps a common indent")

    def test_parameters_named_like_keywords_are_kept(self):
        for name, parameter in (("browser_computed_style", "properties"),):
            generated = self.generated[name]["inputSchema"]
            untitled = server._untitled_schema(generated)["properties"][parameter]
            self.assertEqual(untitled, {key: value for key, value in generated["properties"][parameter].items()
                                        if key != "title"})
            self.assertIn(parameter, self.published[name]["inputSchema"]["properties"])

    def test_schemas_accept_and_reject_the_same_arguments(self):
        for name, generated in self.generated.items():
            with self.subTest(tool=name):
                before = generated["inputSchema"]
                after = server._untitled_schema(before)
                jsonschema.Draft202012Validator.check_schema(after)
                old = jsonschema.Draft202012Validator(before)
                new = jsonschema.Draft202012Validator(after)
                verdicts = []
                for case in arguments(before):
                    verdict = old.is_valid(case)
                    self.assertEqual(new.is_valid(case), verdict, case)
                    verdicts.append(verdict)
                self.assertIn(True, verdicts)
                self.assertIn(False, verdicts)


if __name__ == "__main__":
    unittest.main()
