"""Report the compact JSON size of Kitsune's enabled MCP tool definitions.

    uv run python scripts/tool_schema_size.py           # table, with deltas vs the snapshot
    uv run python scripts/tool_schema_size.py --json    # machine-readable sizes
    uv run python scripts/tool_schema_size.py --update  # rewrite the committed snapshot

Tool definitions are resent with every model request, so their size is per-request
context (#88, #31). Sizes are characters of compact JSON (no whitespace, non-ASCII
kept) for every core tool (KITSUNE_CAPS=core, KITSUNE_TOOL_PRESET=full): `total` is
the whole definition as listed by tools/list, `description` and `inputSchema` are
those fields alone. Characters are not tokenizer counts. Some clients send only the
first paragraph of each description (#135), so `firstParagraph` is that part of the
description and `firstParagraphTotal` the definition as such a client sends it. The
default scope is the lean preset, reported as a subset of the full core group.
tests/test_tool_schema_size.py fails when the sizes differ from tests/tool_schema_sizes.json,
so every change to the definitions shows up in review as a snapshot diff, and when a
total or a single tool grows past its budget below, in either view, which only a
deliberate bump raises.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent.parent / "tests" / "tool_schema_sizes.json"

# browser-mcp-bench's Kitsune UI allowlist (browser_mcp_bench/miniwob.py UI_TOOLS["kitsune"]).
# Its devtools-only names (focus, double_click, drag) are not registered by KITSUNE_CAPS=core.
BENCH_UI_TOOLS = (
    "browser_status", "browser_tabs", "browser_page_info", "browser_page_text",
    "browser_forms", "browser_dom", "browser_snapshot", "browser_screenshot", "browser_navigate",
    "browser_click", "browser_type", "browser_scroll", "browser_hover", "browser_find",
    "browser_select_option", "browser_check", "browser_focus", "browser_keypress",
    "browser_double_click", "browser_submit_form", "browser_form_fill", "browser_drag",
)

# Ceilings in compact JSON characters (#88). Growth past one fails the size test; raise it
# deliberately, with the reason in the change, rather than alongside a snapshot update.
# Set about 5% above the sizes once nullable optionals and empty defaults were dropped (#142);
# default (lean) and bench allowlist raised by 100 for the agent-owned tab rules (#139);
# all three raised for browser_dom scoping (#160), bench also for its earlier overage.
# Tightened again once the shared action contract was documented once (#135): lean and
# full core back to their values before #160, bench just above its size. First-paragraph
# views set about 5% above the sizes once first paragraphs became self-contained (#135).
TOTAL_BUDGETS = {"default (lean)": 13_300, "full core": 25_500, "bench allowlist": 17_700}
FIRST_PARAGRAPH_BUDGETS = {"default (lean)": 11_200, "full core": 23_100, "bench allowlist": 15_700}
TOOL_BUDGET = 3_000  # any single core tool, full view


def _compact(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def first_paragraph(description: str) -> str:
    """The description up to its first blank line, as truncating clients send it (#135)."""
    return re.split(r"\n[ \t]*\n", description, maxsplit=1)[0]


def measure() -> dict:
    """Sizes of every core tool (KITSUNE_TOOL_PRESET=full), the default lean ones included."""
    os.environ["KITSUNE_CAPS"] = "core"
    os.environ["KITSUNE_TOOL_PRESET"] = "full"
    from kitsune.server import mcp

    tools = {}
    for tool in asyncio.run(mcp.list_tools()):
        definition = tool.model_dump(mode="json", exclude_none=True)
        first = first_paragraph(definition.get("description", ""))
        tools[tool.name] = {
            "description": _compact(definition.get("description", "")),
            "firstParagraph": _compact(first),
            "inputSchema": _compact(definition["inputSchema"]),
            "total": _compact(definition),
            "firstParagraphTotal": _compact({**definition, "description": first}),
        }
    return dict(sorted(tools.items()))


def totals(tools: dict, names=None) -> dict:
    selected = [sizes for name, sizes in tools.items() if names is None or name in names]
    return {field: sum(sizes.get(field, 0) for sizes in selected)
            for field in ("description", "firstParagraph", "inputSchema", "total", "firstParagraphTotal")
            } | {"tools": len(selected)}


def selections() -> dict:
    """Tool names behind each budgeted total; None is every core tool."""
    from kitsune.server import TOOL_PRESETS

    return {"default (lean)": TOOL_PRESETS["lean"], "full core": None, "bench allowlist": BENCH_UI_TOOLS}


# Budgeted views: the field each one totals and its ceilings.
VIEWS = {"full": ("total", TOTAL_BUDGETS), "first paragraph": ("firstParagraphTotal", FIRST_PARAGRAPH_BUDGETS)}


def over_budget(tools: dict) -> list[str]:
    """Totals and tools whose size exceeds TOTAL_BUDGETS, FIRST_PARAGRAPH_BUDGETS or TOOL_BUDGET."""
    problems = [f"{label} ({view}): {size} > budget {budgets[label]}"
                for view, (field, budgets) in VIEWS.items()
                for label, names in selections().items()
                if (size := totals(tools, names)[field]) > budgets[label]]
    return problems + [f"{name}: {sizes['total']} > per-tool budget {TOOL_BUDGET}"
                       for name, sizes in tools.items() if sizes["total"] > TOOL_BUDGET]


def report(tools: dict, snapshot: dict) -> str:
    def delta(now: int, before: int | None) -> str:
        return "new" if before is None else f"{now - before:+d}" if now != before else ""

    lean = selections()["default (lean)"]
    lines = [f"{'tool':<32}{'description':>12}{'first ¶':>9}{'inputSchema':>12}{'total':>8}{'Δ':>7}"
             f"{'first ¶ total':>14}{'Δ':>7}"]

    def row(heading: str, now: dict, before: dict) -> str:
        return (f"{heading:<32}{now['description']:>12}{now['firstParagraph']:>9}{now['inputSchema']:>12}"
                f"{now['total']:>8}{delta(now['total'], before.get('total')):>7}"
                f"{now['firstParagraphTotal']:>14}"
                f"{delta(now['firstParagraphTotal'], before.get('firstParagraphTotal')):>7}")

    for name, sizes in tools.items():
        mark = "L" if name in lean else "*" if name in BENCH_UI_TOOLS else " "
        lines.append(row(mark + name, sizes, snapshot.get(name, {})))
    marks = {"default (lean)": "L ", "full core": "", "bench allowlist": "*/L "}
    for label, names in selections().items():
        now, before = totals(tools, names), totals(snapshot, names)
        lines.append(row(f"{marks[label]}{label} ({now['tools']} tools)", now, before)
                     + f"  budgets {TOTAL_BUDGETS[label]} / {FIRST_PARAGRAPH_BUDGETS[label]}")
    lines.append(f"per-tool budget {TOOL_BUDGET} (full view)")
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
