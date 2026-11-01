"""Report the compact JSON size of Kitsune's enabled MCP tool definitions.

    uv run python scripts/tool_schema_size.py           # table, with deltas vs the snapshot
    uv run python scripts/tool_schema_size.py --json    # machine-readable sizes
    uv run python scripts/tool_schema_size.py --update  # rewrite the committed snapshot

Tool definitions are resent with every model request, so their size is per-request
context (#88, #31). Sizes are characters of compact JSON (no whitespace, non-ASCII
kept) for every core tool (KITSUNE_CAPS=core), which are Playwright MCP's core tools:
`total` is the whole definition as listed by tools/list, `description` and `inputSchema`
are those fields alone. Characters are not tokenizer counts. Some clients send only the
first paragraph of each description (#135), so `firstParagraph` is that part of the
description and `firstParagraphTotal` the definition as such a client sends it.
tests/test_tool_schema_size.py fails when the sizes differ from tests/tool_schema_sizes.json,
so every change to the definitions shows up in review as a snapshot diff, and when a
total or a single tool grows past its budget below, in either view, which only a
deliberate bump raises. The server instructions are resent with every request too, so
the table ends with their size in each mode, and the test fails when one grows past
INSTRUCTIONS_BUDGETS (#386).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent.parent / "tests" / "tool_schema_sizes.json"

# Ceilings in compact JSON characters (#88). Growth past one fails the size test; raise it
# deliberately, with the reason in the change, rather than alongside a snapshot update.
# The core tools are Playwright MCP's, published verbatim (playwright_tools.json): set about
# 5% above their size, so refreshing that file shows what Playwright's growth costs. Raised by
# 1,000 for Kitsune's browser_ask_user, which core publishes with browser_resume (#376).
TOTAL_BUDGETS = {"core": 22_300}
FIRST_PARAGRAPH_BUDGETS = {"core": 22_300}
TOOL_BUDGET = 3_000  # any single core tool, full view
# Characters of the server instructions in each KITSUNE_MODE, with KITSUNE_HOLD_VIEWED_TAB and
# KITSUNE_PROTECT_USER_TABS on, as by default, which makes them longest. Set about 5% above
# their size after #386 and #405 shortened them.
INSTRUCTIONS_BUDGETS = {"act": 985, "read": 630, "draft": 630}


def _compact(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def first_paragraph(description: str) -> str:
    """The description up to its first blank line, as truncating clients send it (#135)."""
    return re.split(r"\n[ \t]*\n", description, maxsplit=1)[0]


def measure() -> dict:
    """Sizes of every core tool."""
    os.environ["KITSUNE_CAPS"] = "core"
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


def measure_instructions() -> dict:
    """Characters of the server instructions in each mode of INSTRUCTIONS_BUDGETS. The
    instructions are built at import from the environment, so each mode imports the server anew."""
    env = {key: value for key, value in os.environ.items()
           if key not in ("KITSUNE_HOLD_VIEWED_TAB", "KITSUNE_PROTECT_USER_TABS")}
    script = "from kitsune import server; print(len(server.mcp._mcp_server.instructions))"
    return {mode: int(subprocess.run([sys.executable, "-c", script], env={**env, "KITSUNE_MODE": mode},
                                     capture_output=True, text=True, check=True).stdout)
            for mode in INSTRUCTIONS_BUDGETS}


def instructions_over_budget(sizes: dict) -> list[str]:
    """Modes whose instructions exceed INSTRUCTIONS_BUDGETS."""
    return [f"instructions ({mode}): {size} > budget {INSTRUCTIONS_BUDGETS[mode]}"
            for mode, size in sizes.items() if size > INSTRUCTIONS_BUDGETS[mode]]


def totals(tools: dict, names=None) -> dict:
    selected = [sizes for name, sizes in tools.items() if names is None or name in names]
    return {field: sum(sizes.get(field, 0) for sizes in selected)
            for field in ("description", "firstParagraph", "inputSchema", "total", "firstParagraphTotal")
            } | {"tools": len(selected)}


def selections() -> dict:
    """Tool names behind each budgeted total; None is every core tool."""
    return {"core": None}


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

    lines = [f"{'tool':<32}{'description':>12}{'first ¶':>9}{'inputSchema':>12}{'total':>8}{'Δ':>7}"
             f"{'first ¶ total':>14}{'Δ':>7}"]

    def row(heading: str, now: dict, before: dict) -> str:
        return (f"{heading:<32}{now['description']:>12}{now['firstParagraph']:>9}{now['inputSchema']:>12}"
                f"{now['total']:>8}{delta(now['total'], before.get('total')):>7}"
                f"{now['firstParagraphTotal']:>14}"
                f"{delta(now['firstParagraphTotal'], before.get('firstParagraphTotal')):>7}")

    for name, sizes in tools.items():
        lines.append(row(name, sizes, snapshot.get(name, {})))
    for label, names in selections().items():
        now, before = totals(tools, names), totals(snapshot, names)
        lines.append(row(f"{label} ({now['tools']} tools)", now, before)
                     + f"  budgets {TOTAL_BUDGETS[label]} / {FIRST_PARAGRAPH_BUDGETS[label]}")
    lines.append(f"per-tool budget {TOOL_BUDGET} (full view)")
    removed = sorted(set(snapshot) - set(tools))
    if removed:
        lines.append("removed since snapshot: " + ", ".join(removed))
    instructions = measure_instructions()
    lines.append("instructions (characters): " + ", ".join(
        f"{mode} {size} / budget {INSTRUCTIONS_BUDGETS[mode]}" for mode, size in instructions.items()))
    lines += ["OVER BUDGET " + problem for problem in over_budget(tools) + instructions_over_budget(instructions)]
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
