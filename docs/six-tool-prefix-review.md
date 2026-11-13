# Six-tool catalog audit and prefix evidence

Offline reassessment of [#521](https://github.com/junyeong-nero/kitsune/issues/521), 2026-11-13.
**Catalog size is measured here; model-request prefix tokens are not.** No model, vendor application,
user browser or bridge was started for this audit. No runtime/schema/guide trimming is proposed.

## Conclusion

The issue's “about 2.5k more tokens than one-tool surfaces” described an older **14-tool**
configuration and different runs. It is not a current six-tool measurement. The six-tool catalog
is smaller than the old Kitsune catalog, but tool count alone does not rank prefix size; a
single tool can carry a long guide. The available Codex cohort does not record first-request
input or model-request counts, so it cannot answer the current per-request question.

Keep #521 as a **low-priority measurement question**, not a demonstrated current 2.5k-token gap
or a reason to remove more tools. A future token/time claim needs a separately authorized,
frozen comparison with actual model-request usage and recovery costs. The historical verdicts
and the later maintainer adoption decision are unchanged.

## Pins and method

- Current `main`: `dc62b07e8144ca93fa4ff8488a1be031a9230a3d`, after adoption #538 and docs #544.
  Package version remains 0.8.0; the published 0.8.0 release has the older 14-tool default.
- Frozen comparison: old Kitsune `47dafa20a3fddc697a7d991e15d39e4b0232c10d` versus six-tool
  `f5c7a0f85aefd89e621e73665e33d33d79d50676`; Aside CLI/native MCP 1.26.1107.24 and EgoLite
  0.6.2.9. Vendor rows are the **recorded experiment catalogs**, including their delivered
  guides and the Ego image-transport instructions, not fresh vendor probes or a universal minimum.
- Pending guidance stack: #547 → #548 → #549 → #550, ending at
  `5fe903ec4c9d15af2ea24aa023f553e4dfc49f51`. This row is **not main, a merged fix, or a measured
  model cohort**. Independent readonly/handoff PRs #545/#546 are not in that stack.
- Canonical catalog: the full `tools/list` definitions, sorted by name, serialized using
  `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. Include every
  advertised tool, not hidden registered tools, and do not shorten descriptions. Count the
  whole array, including brackets/commas. Count server instructions separately as raw text.
- Description/schema columns sum the compact JSON values of those fields. They exclude the
  field-name keys and other definition metadata; they are not a partition of all catalog bytes.
- Current import-only probe: Python 3.13.5, MCP SDK 1.30.0, Pydantic 2.14.1, isolated temporary
  HOME/KITSUNE_HOME, scripting enabled, default read-only user-tab policy. Socket connections
  were refused. The main and pending checkouts have identical `uv.lock` blobs.
- All **368 recorded before/after catalogs** (92 per arm) match their preflight catalog and
  instruction digests under this normalization. Current main also matches the recorded six-tool
  catalog and instructions. The initial direct list comparison differed only in ordering;
  canonical name-sorted comparison verifies content, not wire-order identity.

These are serialization sizes, **not tokenizer counts, full client prompts or wire usage**.
Task/system prompts, client tool-name wrappers, schema transformations, description elision,
images, conversation history and cache accounting are not represented by this metric. A null
vendor instruction field means no separate instruction text in that record, not zero total
system/prefix input. No token/byte conversion or cost/quota estimate is used.

## Offline measurements

All serialized catalog text in these pins is ASCII, so character and UTF-8 byte totals coincide.

| Surface | Tools | Catalog chars / bytes | Description JSON chars | Input-schema JSON chars | Separate instruction chars / bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| Frozen old Kitsune | 14 | 9,703 | 2,062 | 5,302 | 564 |
| Frozen six-tool = current main | 6 | 6,673 | 3,058 | 2,526 | 750 |
| Frozen Aside | 1 | 6,980 | 6,402 | 491 | 0 (null field) |
| Frozen EgoLite | 1 | 23,425 | 23,210 | 162 | 0 (null field) |
| Pending guidance stack | 6 | 7,075 | 3,460 | 2,526 | 750 |

- Main's catalog is **31.2% smaller** than the old Kitsune catalog. Catalog plus raw instruction
  text is 7,423 versus 10,267 bytes (**27.7% smaller**). Neither percentage is a token saving.
- Against recorded Aside, main's catalog alone is 307 bytes smaller, but adding Kitsune's
  separate instructions makes that limited sum 443 bytes larger. This is why a “one tool versus
  six” or catalog-only ranking is not a full-prefix comparison.
- Schemas are **37.9%** and descriptions **45.8%** of main's catalog characters under this metric.
  The older issue comment's roughly 66% schema share is not the current six-tool composition.
- The pending guidance stack adds **402 characters/bytes**, all in the Python tool description:
  +6.0% catalog, +5.4% catalog-plus-instructions. Its schema and instruction sizes do not change.
  Correctness guidance has a discovery cost; functional tests do not establish a net token win.

Current six-tool breakdown (compact definition values; array overhead is another seven characters):

| Tool | Definition chars | Description JSON chars | Input-schema JSON chars |
| --- | ---: | ---: | ---: |
| `browser_ask_user` | 641 | 154 | 302 |
| `browser_resume` | 736 | 142 | 411 |
| `browser_run_python` | 2,811 | 2,268 | 359 |
| `browser_tabs` | 709 | 33 | 520 |
| `browser_take_screenshot` | 963 | 125 | 665 |
| `browser_tools_call` | 806 | 336 | 269 |

The pending Python definition is 3,213 characters, including a 2,670-character serialized
description. The other five definitions are unchanged. Do not remove trust, ownership,
approval, no-replay or method-contract guidance just to restore the smaller number.

Canonical catalog SHA-256 values:

```text
old Kitsune  dd67bd2f9378ce4f74383b64ddd3eb21e4e51644bdf3995062191f7f78cb44b2
main / six   8a95823d81e6a6f51067a23a487fb1753e285c64743c94216a6da3e05a73e6ea
Aside        f7d87cf1fcd17e3b412e6be64efbc9a7b9d1ae1c16bea323bc8bb5fea594e12c
EgoLite      80f85339b57ba3433d3bf19ae06151f71707e4f2bb16b54edb4d97a2d8cab3c4
pending      35aa1e0bb08054fbb97562598ebc562136b8ea443bf04f6897ed9cde35503965
```

## What the existing model evidence says

These are separate historical cohorts, not additional executions or pooled samples:

1. **Original #521 / S3:** legacy versus the then-14-tool scripting surface had median first
   input 10,582 → 7,574. Aside 5,050 and Ego 4,820 came from a same-day exhibition, not the same
   paired A/B. This is the source of the old headline, not a current cross-vendor prefix estimate.
2. **#523 schema-trim A/B:** first-request median 7,638 → 6,696 (−942), input ratio 0.882, with
   40/40 tasks successful per arm. That experiment supported that specific 14-tool text trim;
   it does not authorize extrapolating its character/token relationship to a new client/catalog.
3. **Stopped expanded Opus A/B:** primary first-request medians 6,720 → 5,837 (−13.1%), but the
   28 completed primary pairs had input ratio 1.216 and elapsed ratio 3.121. The cohort stopped
   at attempt 85 of 120; its timeout usage remains missing. A smaller first request was not a
   sufficient outcome, and the incomplete cohort is not repaired or resumed here.
4. **Independent 184-attempt Codex comparison:** all four arms 46/46 raw task successes. Six-tool
   aggregate input was 2.9% below old Kitsune and adjusted elapsed 4.8% higher; it missed the
   original ≥10% input-saving gate. Adoption was a later maintainer choice, not a new measurement.
   Model requests and first-request input are **unavailable**, not zero or inferable from MCP
   calls. Aside used less input in 36/46 paired cells despite the candidate's lower aggregate.

See [the benchmark report](benchmark.md) for environment/permission differences and other limits.
Cache-inclusive input is not billed cost or subscription quota; summed per-agent elapsed is not
whole-experiment wall time. Raw grades do not establish human-approval coverage or confinement.
The pending bug/guidance fixes have functional regressions only, not new model-efficiency evidence.

## Reproduce the local catalog measurement

Run the following Python with `uv run --locked python` in the desired checked-out revision.
It imports/list-defines tools only; it does not initialize a running MCP session, connect a bridge,
read browser state, or call a model. Use a fresh process for each revision. The output's digest
and counts, not local paths or environment values, are the public audit data.

```python
import asyncio
import hashlib
import json
import os
import socket
import tempfile


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def refuse(*args, **kwargs):
    raise AssertionError("Offline catalog audit must not connect to a socket")


socket.socket.connect = socket.socket.connect_ex = refuse
with tempfile.TemporaryDirectory() as home:
    for key in list(os.environ):
        if key.startswith("KITSUNE_"):
            del os.environ[key]
    os.environ.update(HOME=home, KITSUNE_HOME=home, KITSUNE_SCRIPTING="1")
    from kitsune import server

    tools = sorted((t.model_dump(mode="json", exclude_none=True)
                    for t in asyncio.run(server.mcp.list_tools())), key=lambda t: t["name"])
    catalog = compact(tools)
    instructions = server.mcp._mcp_server.instructions or ""
    print(json.dumps({
        "tools": len(tools), "catalog_chars": len(catalog),
        "catalog_bytes": len(catalog.encode("utf-8")),
        "description_chars": sum(len(compact(t.get("description", ""))) for t in tools),
        "schema_chars": sum(len(compact(t["inputSchema"])) for t in tools),
        "instructions_chars": len(instructions),
        "instructions_bytes": len(instructions.encode("utf-8")),
        "catalog_sha256": hashlib.sha256(catalog.encode("utf-8")).hexdigest(),
    }, indent=2))
```

The existing `scripts/tool_schema_size.py` deliberately forces `KITSUNE_SCRIPTING=0` and measures
**27 legacy core tools**, excluding the gateway tools. Its snapshot/budget tests passing is not a
six-tool size budget. This reassessment changes neither those historical budgets nor current tool
visibility. A future default-catalog size guard should measure actual `tools/list`, not reuse that
legacy-only total or declare a byte threshold equivalent to an efficiency gate.

Sources: benchmark report/audit commit `0c694d4a74fc1d0fba9d05c9c7a139aa5e697ce2`,
`docs/results/2026-11-11-kitsune-scripting-surface-ab.md`,
`docs/results/2026-11-11-kitsune-521-schema-trim-ab.md`,
`docs/results/2026-11-12-code-first-expanded-ab.md`, and
`docs/results/2026-11-13-expanded-four-way-codex.md`. The latter cohort's saved catalog preflight
and before/after records were read offline; private raw transcripts, profiles, screenshots and
session/approval tokens are not published here.
