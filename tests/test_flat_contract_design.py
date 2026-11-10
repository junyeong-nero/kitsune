"""Offline #499 design checks, NOT runner/security/browser conformance evidence.

Execute only the authored documentation examples against synthetic replies. This
fixture interpreter is deliberately NOT a sandbox and must never run agent code.
"""
import ast
import asyncio
from copy import deepcopy
import inspect
import json
import re
import subprocess
import unittest

from jsonschema import Draft202012Validator, ValidationError
from mcp.types import Tool
from scripts.flat_contract import DESIGN, normalize_call, sdk_call, sdk_signatures, specification, tool_definitions


class ExampleOperationError(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__(result["error"]["code"])


class ExampleBrowser:
    """Compare a documented SDK request with the corresponding direct request."""
    def __init__(self, case):
        self.case = case
        self.trace, self.results, self.emitted, self.images = [], [], [], []
        self.approved = False

    def __getattr__(self, method):
        async def call(**arguments):
            index = len(self.trace)
            step = self.case["direct"][index]
            expected = normalize_call(step["tool"], step["arguments"], self.case["mode"])
            actual = sdk_call(method, arguments, self.case["mode"])
            if actual != expected:
                raise AssertionError(f"Example trace differs: {actual!r} != {expected!r}")
            if step.get("requires_external_event") and not self.approved:
                raise AssertionError("Example tried to resume without its external user event")
            self.trace.append(actual)
            result = {"ok": True, "call": f"c_{index}", "operation": step["tool"].removeprefix("browser_"),
                      "outcome": "dispatched", "data": {}, "truncated": False, **deepcopy(step["reply"])}
            self.results.append(result)
            if not result["ok"]:
                raise ExampleOperationError(result)
            return result
        return call

    def emit_image(self, handle):
        if not any(result["data"].get("image") == handle for result in self.results):
            raise AssertionError("Image must come from this example's screenshot reply")
        self.images.append(handle)

    async def run(self):
        for index, source in enumerate(self.case["python"]):
            if any(event["before_program"] == index for event in self.case.get("external_events", [])):
                self.approved = True  # Synthetic user event in the test, never in the program.
            namespace = {"browser": self, "emit": self.emitted.append, "emit_image": self.emit_image,
                         "BrowserOperationError": ExampleOperationError}
            code = compile(source, f"{self.case['id']}:{index}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            await eval(code, namespace)  # Fresh variables each program; no real browser is imported.


def examples():
    return json.loads((DESIGN / "499-examples.json").read_text())["cases"]


class FlatContractDesignTests(unittest.TestCase):
    def test_all_expanded_schemas_are_valid_and_self_contained(self):
        for mode in ("read", "draft", "act"):
            with self.subTest(mode=mode):
                tools = tool_definitions(mode)
                self.assertEqual(len(tools), len({tool["name"] for tool in tools}))
                for tool in tools:
                    Draft202012Validator.check_schema(tool["inputSchema"])
                    self.assertNotIn('"$ref"', json.dumps(tool))
                    self.assertEqual(set(tool), {"name", "description", "inputSchema", "annotations"})
                    self.assertEqual(Tool.model_validate(tool).model_dump(mode="json", exclude_none=True), tool)
                    if tool["name"] in {"browser_tabs", "browser_scroll", "browser_script", "browser_screenshot"}:
                        self.assertFalse(tool["annotations"]["readOnlyHint"])

    def test_modes_narrow_branches_not_just_tool_names(self):
        invalid = [
            ("browser_tabs", {"action": "close", "tab_id": "t_one"}),
            ("browser_navigate", {"action": "back", "tab_id": "t_one"}),
            ("browser_wait", {"tab_id": "t_one", "condition": {"kind": "url", "value": "https://example.test"}}),
            ("browser_screenshot", {"tab_id": "t_one"}),
            ("browser_resume", {"call": "c_one", "resume_token": "r_one"}),
            ("browser_request_tab", {"tab_id": "t_one"}),
        ]
        for mode in ("read", "draft"):
            for name, args in invalid:
                with self.subTest(mode=mode, tool=name), self.assertRaises((ValueError, ValidationError)):
                    normalize_call(name, args, mode)
            normalize_call("browser_tabs", {"action": "open"}, mode)
            normalize_call("browser_wait", {"tab_id": "t_one", "condition": {"kind": "text", "value": "Ready"}}, mode)
        with self.assertRaises(ValueError):
            sdk_call("input", {"tab_id": "t_one", "target": {"ref": "e1"}, "text": "draft"}, "read")
        sdk_call("input", {"tab_id": "t_one", "target": {"ref": "e1"}, "text": "draft"}, "draft")

    def test_c_and_d_differ_only_by_the_runner_definition(self):
        for mode in ("read", "draft", "act"):
            c = tool_definitions(mode, scripting=False)
            d = tool_definitions(mode, scripting=True)
            self.assertEqual(c, [tool for tool in d if tool["name"] != "browser_script"])
            self.assertEqual(len(d) - len(c), 1)

    def test_every_sdk_signature_matches_one_concrete_schema_branch(self):
        signatures = sdk_signatures()
        bound_names = []
        definitions = {entry["name"]: entry["inputSchema"] for entry in tool_definitions()}
        for entry in specification()["tools"]:
            for method, constants in entry["sdk"].items():
                bound_names.append(method)
                schema = definitions[entry["name"]]
                if "oneOf" in schema:
                    schema = next(branch for branch in schema["oneOf"]
                                  if Draft202012Validator(branch["properties"]["action"]).is_valid(constants["action"]))
                parameters = {name: param for name, param in signatures[method].parameters.items() if name != "self"}
                with self.subTest(method=method):
                    self.assertEqual(set(parameters), set(schema["properties"]) - set(constants))
                    self.assertEqual({name for name, param in parameters.items() if param.default is inspect.Parameter.empty},
                                     set(schema.get("required", [])) - set(constants))
                    for name, param in parameters.items():
                        self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)
                        if param.default is not inspect.Parameter.empty and param.default is not None:
                            self.assertEqual(param.default, schema["properties"][name]["default"])
        self.assertEqual(set(bound_names), set(signatures))
        self.assertEqual(len(bound_names), len(set(bound_names)))
        self.assertNotIn("script", signatures)
        self.assertNotIn("connect", signatures)
        tree = ast.parse((DESIGN / "499-sdk.pyi").read_text())
        browser = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Browser")
        self.assertTrue(all(isinstance(method, ast.AsyncFunctionDef) and len(method.body) == 1
                            and isinstance(method.body[0], ast.Expr) and method.body[0].value.value is Ellipsis
                            for method in browser.body))

    def test_js_declarations_parse_and_have_the_same_parameters(self):
        path = DESIGN / "499-sdk.d.ts"
        subprocess.run(["node", "--input-type=module", "-e",
                        "import {stripTypeScriptTypes} from 'node:module'; import {readFileSync} from 'node:fs';"
                        "stripTypeScriptTypes(readFileSync(process.argv[1], 'utf8'));", str(path)],
                       check=True, capture_output=True, text=True)
        declarations = {"tabs": set()}
        for name, properties in re.findall(r"^  (\w+)\(options\??: \{([^}]+)\}\): Promise<Result>;", path.read_text(), re.M):
            declarations[name] = set(re.findall(r"(\w+)\??:", properties))
        self.assertEqual(declarations, {name: set(signature.parameters) - {"self"}
                                        for name, signature in sdk_signatures().items()})

    def test_defaults_and_special_cases_are_explicit(self):
        self.assertEqual(sdk_call("new_tab", {})["arguments"],
                         {"action": "open", "url": "about:blank", "timeout_ms": 15000})
        self.assertEqual(sdk_call("query", {"tab_id": "t_one", "selector": "a"})["arguments"]["fields"], ["text"])
        self.assertEqual(sdk_call("scroll", {"tab_id": "t_one"})["arguments"]["amount_px"], 500)
        self.assertNotIn("amount_px", sdk_call("scroll", {"tab_id": "t_one", "direction": "bottom"})["arguments"])
        sdk_call("screenshot", {"tab_id": "t_one", "scope": "element", "target": {"ref": "e1"}})
        sdk_call("ask_user", {"reason": "other", "message": "Which option?"}, "read")

    def test_invalid_ambiguous_or_authority_shaped_arguments_are_rejected(self):
        invalid = [
            ("browser_read", {"tab_id": 1}),
            ("browser_read", {"tab_id": "t_one", "format": "html"}),
            ("browser_click", {"tab_id": "t_one", "target": {"ref": "e1", "selector": "button"}}),
            ("browser_click", {"tab_id": "t_one", "target": {"ref": "e1"}, "first": True}),
            ("browser_click", {"tab_id": "t_one", "target": {"ref": "e1"}, "click_count": 2, "button": "right"}),
            ("browser_input", {"tab_id": "t_one", "target": {"ref": "e1"}, "text": "x", "mode": "act"}),
            ("browser_query", {"tab_id": "t_one", "selector": "a", "fields": ["__proto__"]}),
            ("browser_wait", {"tab_id": "t_one", "condition": {"kind": "js", "value": "true"}}),
            ("browser_wait", {"tab_id": "t_one", "condition": {"kind": "text", "value": "x"}, "timeout_ms": 0}),
            ("browser_screenshot", {"tab_id": "t_one", "scope": "element"}),
            ("browser_screenshot", {"tab_id": "t_one", "target": {"ref": "e1"}}),
            ("browser_screenshot", {"tab_id": "t_one", "filename": "/tmp/arbitrary.png"}),
            ("browser_scroll", {"tab_id": "t_one", "direction": "top", "amount_px": 500}),
            ("browser_request_tab", {"tab_id": "t_one", "approved": True}),
            ("browser_ask_user", {"reason": "sign_in", "message": "Please sign in"}),
            ("browser_resume", {"call": "c_one", "resume_token": "r_one", "observed": {"0": "not_applied"}}),
            ("browser_script", {"code": "pass", "headers": {"X-Kitsune-Mode": "act"}}),
            ("browser_script", {"code": "pass", "language": "javascript"}),
        ]
        for name, arguments in invalid:
            with self.subTest(tool=name, arguments=arguments), self.assertRaises(ValidationError):
                normalize_call(name, arguments)
        with self.assertRaisesRegex(ValueError, "source_too_large"):
            normalize_call("browser_script", {"code": "😀" * 9000})
        with self.assertRaises(TypeError):
            sdk_call("read", {"tab_id": "t_one", "headers": {}})

    def test_every_direct_disposition_names_a_proposed_tool(self):
        mapping = json.loads((DESIGN / "499-capability-map.json").read_text())
        names = {tool["name"] for tool in tool_definitions()}
        referred = set()
        for group in mapping["groups"]:
            if group["destination"] == "direct":
                replacements = set(re.findall(r"browser_[a-z_]+", group["replacement"]))
                self.assertTrue(replacements, group["id"])
                self.assertLessEqual(replacements, names)
                referred.update(replacements)
        self.assertEqual(referred, names - {"browser_script"})

    def test_six_paired_examples_have_identical_requests_and_declared_output(self):
        cases = examples()
        self.assertEqual({case["id"] for case in cases}, {"read-page", "fill-draft", "multi-tab-research",
                                                        "repeated-table-form", "screenshot", "user-tab-handoff"})
        for case in cases:
            with self.subTest(case=case["id"]):
                browser = ExampleBrowser(case)
                asyncio.run(browser.run())
                self.assertEqual(browser.trace, [normalize_call(step["tool"], step["arguments"], case["mode"])
                                                 for step in case["direct"]])
                self.assertEqual(browser.emitted, case["expected_emits"])
                self.assertEqual(browser.images, case["expected_images"])
                if case["id"] == "user-tab-handoff":
                    self.assertEqual(sum(not result["ok"] for result in browser.results), 1)

    def test_repeated_work_stops_without_replay_after_uncertain_write(self):
        case = deepcopy(next(case for case in examples() if case["id"] == "repeated-table-form"))
        case["direct"][2]["reply"] = {"ok": False, "outcome": "unknown", "data": {},
                                        "error": {"code": "timeout", "retryable": False}}
        browser = ExampleBrowser(case)
        with self.assertRaises(ExampleOperationError):
            asyncio.run(browser.run())
        self.assertEqual(len(browser.trace), 3)
        self.assertTrue(browser.results[1]["ok"])
        self.assertEqual(browser.results[2]["outcome"], "unknown")
        self.assertEqual(browser.emitted, [])

    def test_truncated_table_causes_no_writes(self):
        case = deepcopy(next(case for case in examples() if case["id"] == "repeated-table-form"))
        case["direct"][0]["reply"]["truncated"] = True
        browser = ExampleBrowser(case)
        with self.assertRaisesRegex(ValueError, "narrower"):
            asyncio.run(browser.run())
        self.assertEqual(len(browser.trace), 1)


if __name__ == "__main__":
    unittest.main()
