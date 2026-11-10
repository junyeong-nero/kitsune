"""Offline #504 preregistration checks, NOT conformance, security or benchmark evidence.

They compare the protocol document with its companion JSON and the #499 design
artifacts. No model, browser, bridge or network is used, and the mirrored
kitsune-benchmark values are not verified against that repository here.
"""
import hashlib
from itertools import pairwise
import json
import re
import unittest

from scripts.flat_contract import DESIGN, tool_definitions

DOCUMENT = DESIGN / "504-validation-protocol.md"


def companion():
    return json.loads((DESIGN / "504-validation-protocol.json").read_text())


def section(text, number):
    """Text of one numbered second-level section of the protocol document."""
    return re.search(rf"^## {number}\. .*?(?=^## \d+\. |\Z)", text, re.M | re.S).group(0)


def prose(text):
    """Collapse line wrapping so a registered sentence can be matched as written."""
    return " ".join(text.split())


class ValidationProtocolDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOCUMENT.read_text()
        cls.companion = companion()

    def test_document_is_the_registered_one(self):
        registered = self.companion["document"]
        self.assertEqual(registered["path"], DOCUMENT.name)
        self.assertEqual(hashlib.sha256(DOCUMENT.read_bytes()).hexdigest(), registered["sha256"])
        for binding in ("benchmark_protocol", "benchmark_preflight_repair"):
            self.assertIn(self.companion["bindings"][binding]["commit"], self.text)
        for key in ("config_sha256", "document_sha256", "schedule_sha256_as_reported_not_recomputed"):
            self.assertIn(self.companion["bindings"]["benchmark_protocol"][key], self.text)

    def test_nothing_is_executed_approved_or_pinned_early(self):
        self.assertIs(self.companion["model_invoked"], False)
        self.assertIs(self.companion["execution_approved"], False)
        self.assertNotIn("results", self.companion)
        self.assertTrue(all(pin["value"] is None for pin in self.companion["pins"].values()))
        self.assertIn("No amendments.", section(self.text, 16))

    def test_pins_triggers_and_layers_match_the_document(self):
        self.assertEqual(set(re.findall(r"PIN-\d\d", self.text)), set(self.companion["pins"]))
        self.assertEqual(set(re.findall(r"PIN-\d\d", section(self.text, 4))), set(self.companion["pins"]))
        self.assertEqual(re.findall(r"^\| (RT-\d) \|", section(self.text, 3), re.M),
                         self.companion["relabel_triggers"])
        self.assertEqual(re.findall(r"^\| (I\d) ", section(self.text, 11), re.M),
                         self.companion["instrumentation_layers"])
        self.assertEqual(re.findall(r"^\| `(\w+)` \|", section(self.text, 12), re.M),
                         self.companion["code_review_classes"])

    def test_every_open_decision_is_marked_once_and_listed(self):
        decisions = list(self.companion["open_decisions"])
        self.assertEqual(sorted(re.findall(r"\*\*OPEN DECISION (OD-\d\d):\*\*", self.text)), decisions)
        self.assertEqual(self.text.count("OPEN DECISION"), len(decisions) + 1)  # plus the marker's definition
        self.assertEqual(re.findall(r"^\| (OD-\d\d) \|", section(self.text, 15), re.M), decisions)
        self.assertEqual(set(re.findall(r"OD-\d\d", self.text)), set(decisions))

    def test_no_model_test_rows_match_the_companion(self):
        rows = dict(re.findall(r"^\| ([BPL]\d\d) \|.*\| (\w+) \|$", section(self.text, 10), re.M))
        self.assertEqual(rows, self.companion["deterministic_tests"])
        self.assertLessEqual(set(re.findall(r"\b[BPL]\d\d\b", self.text)), set(rows))
        for first, last in re.findall(r"\b([BPL]\d\d)–([BPL]\d\d)\b", self.text):
            self.assertEqual(first[0], last[0])
            self.assertLess(first, last)

    def test_arms_differ_only_as_declared(self):
        arms = self.companion["arms"]
        self.assertEqual(arms["A"]["source"], arms["B"]["source"])
        self.assertEqual(arms["C"]["source"], arms["D"]["source"])
        self.assertNotEqual(arms["A"]["source"], arms["C"]["source"])
        self.assertEqual({key for key in arms["C"]["env"] if arms["C"]["env"][key] != arms["D"]["env"][key]},
                         {"KITSUNE_SCRIPTING"})
        for legacy in ("A", "B"):
            self.assertIsNone(arms[legacy]["env"]["KITSUNE_INTERFACE"])
            self.assertIsNone(arms[legacy]["env"]["KITSUNE_SCRIPTING"])
        for flat in ("C", "D"):
            self.assertIsNone(arms[flat]["env"]["KITSUNE_CAPS"])
            self.assertIsNone(arms[flat]["env"]["KITSUNE_PUBLISH"])
        for setting, value in self.companion["common_env"].items():
            self.assertIn(f"`{setting}={value}`", self.text)

    def test_design_stage_footprints_are_the_499_measurements(self):
        costs = json.loads((DESIGN / "499-interface-costs.json").read_text())["rows"]
        table = section(self.text, 3)
        measured = [costs[self.companion["arms"][arm]["interface_cost_row"]] for arm in "ABCD"]
        self.assertIn("| Tools listed at start, `act` | " + " | ".join(str(row["count"]) for row in measured) + " |", table)
        self.assertIn("| Initial definitions + guides | " + " | ".join(f"{row['sum']:,}" for row in measured) + " |", table)
        self.assertEqual([measured[2]["count"], measured[3]["count"]],
                         [len(tool_definitions("act", scripting=False)), len(tool_definitions("act", scripting=True))])

    def test_schedule_is_counterbalanced_and_counted(self):
        design, units = self.companion["design"], self.companion["units"]
        orders = design["arm_orders"]
        self.assertEqual(len(orders), design["repetitions"])
        for position in range(4):
            self.assertEqual({order[position] for order in orders}, set("ABCD"))
        adjacent = [pair for order in orders for pair in pairwise(order)]
        self.assertEqual(len(adjacent), len(set(adjacent)))
        self.assertEqual(len(adjacent), 12)
        personal = len(units["personal_categories"]) * units["tasks_per_personal_category"]
        total = personal + len(units["additional"])
        primary = personal + sum(unit["primary"] for unit in units["additional"])
        repeats = design["repetitions"]
        schedule = prose(section(self.text, 7))
        self.assertIn(f"{total} × {repeats} × 4 = **{total * repeats * 4} runs**", schedule)
        self.assertIn(f"{total * repeats} per arm", schedule)
        self.assertIn(f"{primary} units, {primary * repeats} runs per arm", prose(section(self.text, 5)))
        for order in orders:
            self.assertIn(f"`{order}`", schedule)
        self.assertIn(design["unit_order_seed"], schedule)

    def test_mirrored_limits_and_thresholds_are_stated(self):
        design, decision = self.companion["design"], self.companion["decision"]
        budget, gates = prose(section(self.text, 8)), prose(section(self.text, 9))
        self.assertIn(f"{design['campaign_active_hours']} active campaign hours", budget)
        self.assertIn(f"${design['list_price_review_stop_usd']} of accumulated", budget)
        self.assertIn("Zero automatic retries", budget)
        self.assertEqual(design["automatic_retries"], 0)
        frozen = prose(section(self.text, 4))
        self.assertIn(f"{design['timeouts_sec']['personal']} s personal and typing; "
                      f"{design['timeouts_sec']['gallery']} s gallery", frozen)
        self.assertIn(f"{design['startup_timeout_sec']} s and {design['cleanup_timeout_sec']} s", frozen)
        self.assertIn(f"at least {decision['minimum_primary_success_rate']:.0%} of their 116 primary runs", gates)
        self.assertIn(f"The allowed count loss is {decision['maximum_success_count_loss_per_unit']}.", gates)
        self.assertIn(f"least {decision['minimum_joint_primary_fraction']:.0%} of primary keys", gates)
        self.assertIn(f"all {decision['minimum_primary_categories_with_joint_success']} categories", gates)
        self.assertIn(f"all {decision['minimum_simple_units_with_joint_success']} simple units", gates)
        self.assertIn(f"all {decision['minimum_repetition_units_with_joint_success']} composition units", gates)
        self.assertIn(f"at least {decision['minimum_joint_per_primary_unit']} joint success", gates)
        ratios = [("D/A", "primary", "default_D_to_A_cost_max"),
                  ("D/B", "primary", "default_D_to_B_cost_max"),
                  ("D/C", "composition stratum", "scripting_D_to_C_repetition_cost_max"),
                  ("D/C", "primary", "scripting_D_to_C_all_primary_cost_max"),
                  ("D/A and D/C", "simple stratum", "simple_D_to_A_and_C_cost_max"),
                  ("D/A and D/C, wall time", "primary, end to end", "primary_D_to_A_and_C_wall_time_max"),
                  ("D/A and D/C, all attempts", "cost per successful primary task",
                   "all_attempt_cost_per_success_D_to_A_and_C_max")]
        for ratio, subset, key in ratios:
            self.assertIn(f"| {ratio} | {subset} | ≤ {decision[key]:.2f} |", gates)
        self.assertIn(f"at least {decision['required_repetitions_in_favorable_direction']} of 4 repetitions", gates)
        self.assertIn(f"one-sided {decision['bootstrap_upper_quantile']:.0%} cluster-bootstrap", gates)
        self.assertIn(f"{decision['bootstrap_samples']:,} times with seed {decision['bootstrap_seed']}", gates)
        self.assertEqual(decision["maximum_candidate_safety_violations"], 0)

    def test_all_three_outcomes_findings_and_checkboxes_are_present(self):
        gates = prose(section(self.text, 9))
        for outcome in self.companion["outcomes"]:
            self.assertIn(f"**{outcome}", gates)
        for finding in self.companion["findings"]:
            self.assertIn(finding, gates)
        self.assertEqual(re.findall(r"^\| (\d)\. ", section(self.text, 14), re.M),
                         [str(number) for number in range(1, self.companion["acceptance_checkboxes"] + 1)])


if __name__ == "__main__":
    unittest.main()
