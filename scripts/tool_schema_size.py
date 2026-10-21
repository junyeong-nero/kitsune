"""Report the compact JSON size of Kitsune's enabled MCP tool definitions.

    uv run python scripts/tool_schema_size.py           # table, with deltas vs the snapshot
    uv run python scripts/tool_schema_size.py --json    # machine-readable sizes
    uv run python scripts/tool_schema_size.py --update  # rewrite the committed snapshot

Tool definitions are resent with every model request, so their size is per-request
context (#88, #31). Sizes are characters of compact JSON (no whitespace, non-ASCII
kept) for the default capability set (KITSUNE_CAPS=core): `total` is the whole
definition as listed by tools/list, `description` and `inputSchema` are those
fields alone. Characters are not tokenizer counts. tests/test_tool_schema_size.py
fails when the sizes differ from tests/tool_schema_sizes.json, so every change
to the definitions shows up in review as a snapshot diff, and when a total or a
single tool grows past its budget below, which only a deliberate bump raises.
The lean preset (KITSUNE_TOOL_PRESET=lean) is reported as a subset of core.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent.parent / "tests" / "tool_schema_sizes.json"

# browser-mcp-bench's Kitsune UI allowlist (browser_mcp_bench/miniwob.py UI_TOOLS["kitsune"]).
# Its devtools-only names (focus, double_click, drag) are not registered by default.
BENCH_UI_TOOLS = (
    "browser_status", "browser_tabs", "browser_page_info", "browser_page_text",
    "browser_forms", "browser_dom", "browser_snapshot", "browser_screenshot", "browser_navigate",
    "browser_click", "browser_type", "browser_scroll", "browser_hover", "browser_find",
    "browser_select_option", "browser_check", "browser_focus", "browser_keypress",
    "browser_double_click", "browser_submit_form", "browser_form_fill", "browser_drag",
)

# Ceilings in compact JSON characters (#88). Growth past one fails the size test; raise it
# deliberately, with the reason in the change, rather than alongside a snapshot update.
# Set about 5% above the sizes once generated schema titles were dropped (#120).
TOTAL_BUDGETS = {"core": 30_000, "bench allowlist": 20_500, "lean preset": 15_000}
TOOL_BUDGET = 3_000  # any single core tool


def _compact(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def measure() -> dict:
    """Sizes of every tool registered with the default capability set."""
    os.environ["KITSUNE_CAPS"] = "core"
    os.environ.pop("KITSUNE_TOOL_PRESET", None)
    from kitsune.server import mcp

    tools = {}
    for tool in asyncio.run(mcp.list_tools()):
        definition = tool.model_dump(mode="json", exclude_none=True)
        tools[tool.name] = {
            "description": _compact(definition.get("description", "")),
            "inputSchema": _compact(definition["inputSchema"]),
            "total": _compact(definition),
        }
    return dict(sorted(tools.items()))


def totals(tools: dict, names=None) -> dict:
    selected = [sizes for name, sizes in tools.items() if names is None or name in names]
    return {field: sum(sizes[field] for sizes in selected)
            for field in ("description", "inputSchema", "total")} | {"tools": len(selected)}


def selections() -> dict:
    """Tool names behind each budgeted total; None is every core tool."""
    from kitsune.server import TOOL_PRESETS

    return {"core": None, "bench allowlist": BENCH_UI_TOOLS, "lean preset": TOOL_PRESETS["lean"]}


def over_budget(tools: dict) -> list[str]:
    """Totals and tools whose size exceeds TOTAL_BUDGETS or TOOL_BUDGET."""
    problems = [f"{label}: {size} > budget {TOTAL_BUDGETS[label]}"
                for label, names in selections().items()
                if (size := totals(tools, names)["total"]) > TOTAL_BUDGETS[label]]
    return problems + [f"{name}: {sizes['total']} > per-tool budget {TOOL_BUDGET}"
                       for name, sizes in tools.items() if sizes["total"] > TOOL_BUDGET]


def report(tools: dict, snapshot: dict) -> str:
    def delta(now: int, before: int | None) -> str:
        return "new" if before is None else f"{now - before:+d}" if now != before else ""

    lean = selections()["lean preset"]
    lines = [f"{'tool':<32}{'description':>12}{'inputSchema':>12}{'total':>8}{'Δ total':>9}"]
    for name, sizes in tools.items():
        mark = "L" if name in lean else "*" if name in BENCH_UI_TOOLS else " "
        before = snapshot.get(name, {}).get("total")
        lines.append(f"{mark}{name:<31}{sizes['description']:>12}{sizes['inputSchema']:>12}"
                     f"{sizes['total']:>8}{delta(sizes['total'], before):>9}")
    marks = {"core": "", "bench allowlist": "*/L ", "lean preset": "L "}
    for label, names in selections().items():
        now, before = totals(tools, names), totals(snapshot, names)
        heading = f"{marks[label]}{label} ({now['tools']} tools)"
        lines.append(f"{heading:<32}{now['description']:>12}"
                     f"{now['inputSchema']:>12}{now['total']:>8}{delta(now['total'], before['total']):>9}"
                     f"  budget {TOTAL_BUDGETS[label]}")
    lines.append(f"per-tool budget {TOOL_BUDGET}")
    removed = sorted(set(snapshot) - set(tools))
    if removed:
        lines.append("removed since snapshot: " + ", ".join(removed))
    lines += ["OVER BUDGET " + problem for problem in over_budget(tools)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print sizes as JSON")
    mode.add_argument("--update", action="store_true", help=f"write sizes to {SNAPSHOT.name}")
    args = parser.parse_args(argv)
    tools = measure()
    if args.json:
        print(json.dumps(tools, indent=2))
    elif args.update:
        SNAPSHOT.write_text(json.dumps(tools, indent=2) + "\n")
        print(f"wrote {SNAPSHOT}")
    else:
        snapshot = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
        print(report(tools, snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
