"""Runtime argument admission vs the independently retained #499 design fixtures."""
from copy import deepcopy
import unittest

from jsonschema import ValidationError

from kitsune.flat_operations import OPERATIONS, OperationRefused, validate_operation
from scripts.flat_contract import normalize_call, tool_definitions


TAB = {"tab_id": "t_fixture"}
TARGET = {**TAB, "target": {"ref": "e12"}}
CASES = [
    ("browser_tabs", {}), ("browser_tabs", {"action": "open"}),
    ("browser_tabs", {"action": "close", **TAB}),
    ("browser_navigate", {**TAB, "url": "https://fixture.test/"}),
    *[("browser_navigate", {**TAB, "action": action}) for action in ("back", "forward", "reload")],
    ("browser_read", TAB), ("browser_read", {**TAB, "format": "text", "target": {"selector": "main"}}),
    ("browser_query", {**TAB, "selector": "a"}),
    ("browser_find", {**TAB, "text": "needle"}),
    ("browser_click", TARGET), ("browser_click", {**TARGET, "click_count": 2}),
    ("browser_input", {**TARGET, "text": "draft"}),
    ("browser_select", {**TARGET, "values": ["one"]}), ("browser_check", TARGET),
    ("browser_press", {**TARGET, "key": "Enter"}), ("browser_hover", TARGET),
    ("browser_drag", {**TAB, "source": {"ref": "e12"}, "destination": {"selector": "#drop"}}),
    ("browser_scroll", TAB), ("browser_scroll", {**TAB, "direction": "top"}),
    *[("browser_wait", {**TAB, "condition": {"kind": kind, "value": "literal"}})
      for kind in ("text", "text_gone", "url", "title")],
    ("browser_wait", {**TAB, "condition": {"kind": "target", "target": {"ref": "e12"}}}),
    ("browser_wait", {**TAB, "condition": {"kind": "network_idle"}}),
    ("browser_screenshot", TAB), ("browser_screenshot", {**TARGET, "scope": "element"}),
    ("browser_request_tab", TAB), ("browser_ask_user", {"reason": "other", "message": "Decide"}),
    ("browser_ask_user", {**TAB, "reason": "sign_in", "message": "Please sign in"}),
    ("browser_resume", {"call": "call-fixture", "resume_token": "opaque-fixture"}),
]


class FlatOperationAdmissionTests(unittest.TestCase):
    def test_every_leaf_operation_and_mode_matches_design_defaults(self):
        self.assertEqual({name for name, _ in CASES}, OPERATIONS)
        self.assertEqual(OPERATIONS, {tool["name"] for tool in tool_definitions(scripting=False)})
        for mode in ("read", "draft", "act"):
            for name, arguments in CASES:
                with self.subTest(mode=mode, name=name, arguments=arguments):
                    try:
                        expected = normalize_call(name, arguments, mode)["arguments"]
                    except (ValueError, ValidationError):
                        with self.assertRaises(OperationRefused) as refused:
                            validate_operation(name, arguments, mode=mode)
                        self.assertEqual(refused.exception.code, "mode_restricted")
                    else:
                        self.assertEqual(validate_operation(name, arguments, mode=mode).arguments(), expected)

    def assert_invalid(self, name, arguments):
        with self.assertRaises(OperationRefused) as refused:
            validate_operation(name, arguments, mode="act")
        self.assertEqual(refused.exception.code, "invalid_arguments")

    def test_authority_and_transport_fields_are_never_operation_arguments(self):
        fields = {"headers": {"X-Kitsune-Mode": "act"}, "endpoint": "/api/set-policy", "policy": {},
                  "ownerId": "other", "client_id": "other", "userTabsWritable": True,
                  "confirm_token": "secret-fixture", "confirmToken": "secret-fixture",
                  "connection": "forged", "caps": "all", "mode": "act", "first": True,
                  "filename": "/tmp/should-not-write"}
        for name, arguments in CASES:
            for key, value in fields.items():
                with self.subTest(name=name, key=key):
                    self.assert_invalid(name, {**arguments, key: value})

    def test_nested_targets_conditions_and_arrays_are_strict(self):
        for target in ({}, {"ref": "e12", "selector": "#x"}, {"selector": ""}, {"ref": "s123e12"},
                       {"ref": "e12", "first": True}, {"ref": None}):
            self.assert_invalid("browser_click", {**TAB, "target": target})
        for condition in ({"kind": "script", "code": "return true"}, {"kind": "text", "value": "", "regex": True},
                          {"kind": "network_idle", "timeout": 1}):
            self.assert_invalid("browser_wait", {**TAB, "condition": condition})
        self.assert_invalid("browser_query", {**TAB, "selector": "a", "fields": ["text", "text"]})
        self.assert_invalid("browser_query", {**TAB, "selector": "a", "fields": ["innerHTML"]})
        self.assert_invalid("browser_click", {**TARGET, "modifiers": ["Shift", "Shift"]})
        self.assert_invalid("browser_select", {**TARGET, "values": ["one", "one"]})

    def test_a_selector_that_the_extension_would_read_as_a_ref_is_refused(self):
        # background.js leadingShortRef and content.js REF_SELECTOR/parseSelectorPart.
        for selector in ("e12", " e12", "e12\n", "\ufeffe12", "\u00a0e12\u3000", "e0", "s17e4", "r3",
                         "e12 >> xpath=..", "e12>>css=button", "e12 >>", "s17e4 >> nth=0", "r3\t>> text=Save"):
            for name, arguments in (
                    ("browser_click", {**TAB, "target": {"selector": selector}}),
                    ("browser_read", {**TAB, "target": {"selector": selector}}),
                    ("browser_drag", {**TAB, "source": {"ref": "e1"}, "destination": {"selector": selector}}),
                    ("browser_drag", {**TAB, "source": {"selector": selector}, "destination": {"ref": "e1"}}),
                    ("browser_wait", {**TAB, "condition": {"kind": "target", "target": {"selector": selector}}}),
                    ("browser_screenshot", {**TAB, "scope": "element", "target": {"selector": selector}})):
                with self.subTest(selector=selector, name=name):
                    self.assert_invalid(name, arguments)
        # Selectors that only resemble one stay selectors, and text is never a selector.
        for selector in ("e12 > span", "e12 span", "e12.item", "em", "e", "E12", "section e12", "#e12", "x >> e12",
                         "text=e12", '"e12"', "e12a", "s17", "re3"):
            with self.subTest(selector=selector):
                validate_operation("browser_click", {**TAB, "target": {"selector": selector}}, mode="act")
        validate_operation("browser_find", {**TAB, "text": "e12"}, mode="act")
        validate_operation("browser_wait", {**TAB, "condition": {"kind": "text", "value": "e12"}}, mode="act")

    def test_explicit_null_raw_tab_ids_and_scalar_coercion_are_refused(self):
        for value in (None, 9, True, "9", ""):
            self.assert_invalid("browser_read", {"tab_id": value})
        for value in (True, False, "1000", 1.25, float("nan"), float("inf"), 0, 60001):
            self.assert_invalid("browser_read", {**TAB, "timeout_ms": value})
        for name, arguments in CASES:
            for key in arguments:
                self.assert_invalid(name, {**arguments, key: None})
        self.assert_invalid("browser_check", {**TARGET, "checked": 1})
        self.assert_invalid("browser_click", {**TARGET, "click_count": True})
        self.assert_invalid("browser_scroll", {**TAB, "amount_px": None})

    def test_integral_json_numbers_match_the_design_integer_type(self):
        for name, arguments, key, value in (
                ("browser_read", TAB, "timeout_ms", 1.0),
                ("browser_read", TAB, "max_chars", 12000.0),
                ("browser_query", {**TAB, "selector": "a"}, "limit", 50.0),
                ("browser_click", TARGET, "click_count", 2.0),
                ("browser_scroll", TAB, "amount_px", 500.0),
                ("browser_resume", {"call": "c", "resume_token": "r"}, "wait_ms", -0.0)):
            supplied = {**arguments, key: value}
            with self.subTest(name=name, key=key):
                expected = normalize_call(name, supplied)["arguments"]
                normalized = validate_operation(name, supplied, mode="act").arguments()
                self.assertEqual(normalized, expected)
                self.assertIs(type(normalized[key]), int)

    def test_cross_field_limits_are_enforced_before_any_adapter(self):
        for arguments in ({**TARGET, "click_count": 2, "button": "right"},
                          {**TARGET, "click_count": 2, "modifiers": ["Shift"]}):
            self.assert_invalid("browser_click", arguments)
        self.assert_invalid("browser_scroll", {**TAB, "direction": "bottom", "amount_px": 500})
        self.assert_invalid("browser_screenshot", TARGET)
        self.assert_invalid("browser_screenshot", {**TAB, "scope": "element"})
        self.assert_invalid("browser_ask_user", {"reason": "human_check", "message": "Help"})
        self.assert_invalid("browser_read", {**TAB, "max_chars": 12001})
        self.assert_invalid("browser_query", {**TAB, "selector": "a", "limit": 201})
        self.assert_invalid("browser_resume", {"call": "c", "resume_token": "r", "wait_ms": 120001})

    def test_unknown_legacy_and_nested_runner_requests_are_refused(self):
        for name in ("browser_script", "browser_evaluate", "browser_tools_call", "/api/click", [], None):
            with self.subTest(name=name), self.assertRaises(OperationRefused) as refused:
                validate_operation(name, {}, mode="act")
            self.assertEqual(refused.exception.code, "unknown_operation")
        for arguments in ([], "{}", None):
            self.assert_invalid("browser_tabs", arguments)
        self.assert_invalid("browser_tabs", {"action": []})

    def test_normalized_arguments_do_not_alias_input_or_previous_output(self):
        arguments = {**TARGET, "target": {"selector": "#one"}, "modifiers": ["Shift"]}
        original = deepcopy(arguments)
        operation = validate_operation("browser_click", arguments, mode="act")
        arguments["target"]["selector"] = "#two"
        arguments["modifiers"].append("Control")
        first = operation.arguments()
        self.assertEqual(first["target"], original["target"])
        self.assertEqual(first["modifiers"], original["modifiers"])
        first["target"]["selector"] = "#three"
        first["modifiers"].clear()
        self.assertEqual(operation.arguments()["target"], original["target"])
        self.assertEqual(operation.arguments()["modifiers"], original["modifiers"])

    def test_refusals_do_not_echo_secret_input(self):
        marker = "private-value-should-not-be-echoed"
        for arguments in ({**TARGET, "text": marker, "mode": marker},
                          {**TARGET, "text": marker, "headers": {"Authorization": marker}}):
            with self.assertRaises(OperationRefused) as refused:
                validate_operation("browser_input", arguments, mode="act")
            self.assertNotIn(marker, str(refused.exception))
            self.assertIsNone(refused.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
