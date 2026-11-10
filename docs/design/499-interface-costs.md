# #499 interface cost measurement — not a benchmark verdict

Measured 2026-11-10; product source `fd9b90e`, prerequisite test fixes from #505.
Candidate definitions/guides/signatures/examples are pinned by SHA-256 in
[499-interface-costs.json](499-interface-costs.json). Reproduce from the repository:

```sh
uv run --locked python -m scripts.flat_contract_size
uv run --locked python -m unittest discover -s tests -p 'test_flat_contract*.py' -v
```

Legacy definitions and initialization instructions come from real server metadata
in fresh private-HOME subprocesses with HTTP requests forbidden. No tools are
called. Candidate definitions are **expanded design fixtures**, validated as MCP
Tool objects, not a claim of an implemented/registered candidate. Local `$ref`
compression is not counted as a context saving. The actual implementation's
served metadata must be remeasured before #504 freezes its manifest.

## Initial components

All table quantities except tool count are **characters, not tokens**. Definitions
are the sum of compact JSON for each complete listed tool, with `ensure_ascii=False`,
matching `scripts/tool_schema_size.py`; array/RPC framing is excluded. For an array
of n nonempty definitions, brackets/commas add n+1 characters; the measurement
script also reports that size. Client/provider framing is not modeled.

For legacy, Guide means its actual initialization instructions. For C/D, Guide is
the proposed common operation guide delivered as instructions; D additionally gets
the entire SDK guide. Neither proposed guide is silently omitted from the sum.
No task text, fixture context, conversation history or model output is included.

| Configuration | Tools | Definitions | Guide | Extra SDK guide | Sum |
| --- | ---: | ---: | ---: | ---: | ---: |
| Legacy core, act | 27 | 21,747 | 537 | 0 | 22,284 |
| Legacy core, read | 9 | 7,117 | 286 | 0 | 7,403 |
| Legacy core, draft | 12 | 9,977 | 286 | 0 | 10,263 |
| Legacy all, publish core, act | 30 | 24,100 | 537 | 0 | 24,637 |
| Legacy all, publish empty, act | 3 | 2,353 | 537 | 0 | 2,890 |
| Flat C direct, act | 18 | 18,163 | 1,928 | 0 | 20,091 |
| Flat D + Python, act | 19 | 18,819 | 1,928 | 3,396 | 24,143 |
| Flat C direct, read | 8 | 7,005 | 1,928 | 0 | 8,933 |
| Flat D + Python, read | 9 | 7,661 | 1,928 | 3,396 | 12,985 |
| Flat C direct, draft | 11 | 9,901 | 1,928 | 0 | 11,829 |
| Flat D + Python, draft | 12 | 10,557 | 1,928 | 3,396 | 15,881 |

Legacy settings are `KITSUNE_CAPS=core` without publication, or `KITSUNE_CAPS=all`
with `KITSUNE_PUBLISH=core` / an explicit empty string. Mode is stated per row;
user tabs remain read-only. `all` still excludes named-only storage/history.
These are footprint reference configurations, **not the frozen #504 B choice**.

### Do not infer a win from this table

- C-act has a smaller initial character sum than legacy core-act; **D-act is larger**.
  Both read/draft candidate sums are larger than their legacy core counterparts.
  Scripting adds 4,052 characters over C in every mode: a 656-character runner
  definition plus the 3,396-character SDK guide. That overhead is real.
- The 3-tool gateway starts much smaller than any candidate. Search replies, later
  publication, tool arguments and extra model requests are not free; they must be
  measured during actual workloads, not assumed to outweigh its small startup.
  Its 100 authorized definitions (97 tools plus 3 gateway tools) total 70,965
  characters / 70,982 UTF-8 bytes, but **are not all initially sent**.
- The interfaces differ in capability coverage and metadata. For a like-field
  projection containing only name/description/inputSchema, core-act is 18,796,
  gateway-core-act 20,767, gateway-only 1,971, C-act 17,502 and D-act 18,121.
  Full rows retain legacy optional metadata and the candidate's explicit
  readOnlyHint. Other legacy annotations/output schemas are not all reproduced;
  clients may use these hints differently. Hints never provide authority.
- Candidate readOnlyHint is false for mixed/mutating tools, screenshots and the
  runner even in read mode; a mode name does not make tab opening/scrolling pure.
  It is true for read/query/find/wait/ask_user. These are conservative client hints,
  not substitutes for dispatch-time enforcement. Final bindings must preserve or
  explicitly review metadata changes and repeat this measurement.
- Clients that keep only the first description paragraph see gateway-core's
  definitions at 23,261 and gateway-only's at 1,514 characters. Core and candidate
  definitions are unchanged in that view. Such client behavior is a #504 variable.
- All currently listed definitions/common guides in the table are ASCII, so their
  UTF-8 byte counts equal characters. The SDK guide is 3,404 UTF-8 bytes for 3,396
  characters. Neither unit determines model tokenization or cache billing.

The first exploratory pass omitted candidate optional annotations: D-act summed
23,445 characters. Self-review added explicit readOnlyHint metadata instead of
reporting that smaller floor as the final candidate cost. No benchmark criterion
or model-run result was changed: neither exists yet.

## Authored example/code cost

These are **hand-authored review examples**, not generated model outputs or timing
measurements. JSON sizes sum `{tool, arguments}` for direct steps, or the outer
browser_script request containing each program. They exclude responses and framing.
The source count is separate so escaped newlines/wrapping do not disappear.

| Example | Direct operations | Programs | Source chars | Direct request JSON | Script request JSON |
| --- | ---: | ---: | ---: | ---: | ---: |
| Read page | 1 | 1 | 69 | 58 | 120 |
| Fill draft | 4 | 1 | 449 | 444 | 503 |
| Multi-tab research | 4 | 1 | 336 | 318 | 392 |
| Repeated table/form | 4 | 1 | 702 | 404 | 761 |
| Screenshot | 1 | 1 | 91 | 63 | 142 |
| User-tab handoff | 4 | 2 | 684 | 252 | 796 |

One program is not one underlying browser operation, and fewer outer calls are
not evidence of fewer model requests, lower cost or faster completion. Here even
some short repeated-work scripts have larger request text. The repeated example's
truncation/type/unknown-write handling must not be removed merely to improve size.
The handoff has an external event between programs, not code approving itself.

Optional reference material, **not part of initial C/D context**, is separately
measured: Python declarations 4,986 characters, intended JS declarations 3,842,
paired review fixtures 9,868. If an agent looks these up, that input cost must be
charged in #504. The delivered guide already maps every SDK method to its visible
direct schema, so reference lookup is not a hidden discovery prerequisite.

No model/tokenizer has been selected or invoked for these measurements. #504 must
freeze the model/client rendering and count actual input/output/cache/code tokens,
requests, calls, underlying operations, cold/warm runtime overhead, time and cost.
These design numbers are neither a go/no-go/inconclusive workload verdict nor
approval to switch defaults or implement JS ahead of Python feasibility.
