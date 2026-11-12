# Benchmark and comparison

## Expanded four-way comparison: 184 attempts

The 2026-11-13 comparison covers **23 conditions × two repeats × four arms**: seven personal
browser conditions, ten isolated UI conditions and six crowded UI conditions. Every arm completed
**46/46 raw task successes**, with complete input and timing records. All-attempt and joint-success
comparisons therefore use the same 46 condition/repeat cells; no failed cell is excluded.

![Cache-inclusive input on the same 46 cells: Kitsune 6-tool 5,730,439; Kitsune 14-tool 5,901,946; Aside 5,923,875; EgoLite 7,775,055. All four arms completed 46/46 attempts.](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/benchmark-efficiency.svg)

### Input and elapsed time

| Arm | Raw task success | Input including cache | Raw elapsed, s | Adjusted elapsed, s |
| --- | ---: | ---: | ---: | ---: |
| Kitsune 6-tool code-first | 46/46 | **5,730,439** | 1,396.084 | 1,324.975 |
| Kitsune previous 14-tool default | 46/46 | 5,901,946 | 1,333.899 | 1,264.414 |
| Aside | 46/46 | 5,923,875 | 1,444.198 | **1,242.744** |
| EgoLite | 46/46 | 7,775,055 | 1,627.307 | 1,478.044 |

Six-tool Kitsune uses **2.9% less total input than 14-tool Kitsune**, **3.3% less than Aside** and
**26.3% less than EgoLite**. These are aggregate cache-inclusive input tokens, not billed cost or
subscription-quota savings. Monetary cost and model-request counts are unavailable for this cohort.

Less input does not mean faster: six-tool adjusted elapsed is **4.8% higher than 14-tool Kitsune**
and **6.6% higher than Aside**, and **10.4% lower than EgoLite**. Raw six-tool elapsed is 4.7% higher
than 14-tool Kitsune. The predeclared adjustment subtracts measured relay setup and post-call
inventory, not actual agent actions, images or approval delays. Both time columns sum individual
agent runs, not whole-experiment wall time or isolated browser-operation latency.

### Conditions and limits

- Codex CLI 0.163.0, requested `gpt-6.1-sol`, medium reasoning, ChatGPT subscription. The same
  requested model and settings apply to all 184 attempts. Traces do not independently attest the
  backend model identity. This is a separate cohort from the older Opus studies below.
- Kitsune runtime pins: `f5c7a0f85aefd89e621e73665e33d33d79d50676` (six tools) and
  `47dafa20a3fddc697a7d991e15d39e4b0232c10d` (14 tools). Both already support Python; this is not
  Python versus no Python. The six-tool runtime was subsequently adopted on `main` in
  [#538](https://github.com/junyeong-nero/kitsune/pull/538), unchanged from the measured candidate;
  it is not part of the 0.8.0 release.
- Aside CLI/native MCP 1.26.1107.24, app 1.0.1022.1, one `repl` tool. EgoLite CLI/app 0.6.2.9,
  one `run_script` tool with the predeclared image-attachment bridge. No Playwright or MiniWoB arm
  was run in this cohort, and no historical results are pooled into the chart.
- Two repeats of variant 0, sequential rotated arm order. Personal limit 180 s, gallery 240 s;
  Kitsune MCP limit 45 s, vendor envelope 130 s around native 120 s scripts. No replacements,
  harness retries, missing final usage, quota stops or outcome-based tuning.
- Kitsune used fresh private Firefox and user-seeded read-only personal tabs. Aside/Ego used
  installed browser profiles and harness-created accessible tabs. Browser engines, effective
  viewports, profiles and ownership protections are **not equivalent**.
- Results vary by task and recovery outliers. Aside uses less input in **36/46 paired cells**
  versus six-tool Kitsune despite its higher aggregate total. Six-tool input beats the old
  default in only **16/46 cells**. Two repeats do not establish statistical superiority.
- The model runs made **zero handoff or approval-recovery calls**. Passing task/safety grades
  do not establish human-approval coverage, equivalent user-tab protection or confinement.
  Python/Node remain trusted host runtimes, not sandboxes.

Source: `kitsune-benchmark/docs/results/2026-11-13-expanded-four-way-codex.md`, report/audit commit
`0c694d4a74fc1d0fba9d05c9c7a139aa5e697ce2`, execution commit
`e5db230a78ad006cfb8a12fbee6621b087a03e07`. The figures and limitations are reproduced here so the
README comparison does not depend on access to the benchmark repository. The older interrupted
85/120 Opus A/B remains separate and incomplete; the new results do not replace its timeout.

## Earlier comparison: five-way MiniWoB

The 2026-11-12 exhibition compares Kitsune's default and legacy surfaces on the **same source
revision**, alongside Playwright MCP, ego lite and Aside. It is seven tasks with two repeats per
arm (70 attempts), not a completed standard-lite suite or an equal-environment leaderboard.

### Shared successful cells

Every row below uses the **same 13 task/repeat cells successful for all five arms**. The one
cell ego failed is removed from every arm. Input includes cache hits; these are token totals,
not billed cost or browser-action counts.

| Arm | Input tokens, shared 13 cells | Raw success, all 14 attempts |
| --- | ---: | ---: |
| Kitsune legacy | 785,115 | 14/14 |
| Playwright MCP | 781,019 | 14/14 |
| Kitsune default | 514,964 | 14/14 |
| ego lite | 458,052 | 13/14 |
| Aside | 361,014 | 14/14 |

- **34.1% less input than Playwright:** `1 − 514964 / 781019`.
- **82.6% of the legacy-to-ego input gap closed:** `(785115 − 514964) / (785115 − 458052)`.
- **63.7% of the legacy-to-Aside input gap closed:** `(785115 − 514964) / (785115 − 361014)`.
- Kitsune still uses **12.4% more input than ego** and **42.6% more than Aside** on this set.

“Gap closed” compares default versus legacy on the same Kitsune commit, not progress across
historical releases. It is not a claim of equivalent performance in every dimension.

### Conditions and limits

- Claude Code 2.1.303, Claude Opus 5.5 (`claude-opus-5-5`), medium reasoning, subscription.
- Kitsune `79eb95594eee57bf429c1594c883f1995294dbc8`: pre-release 0.8.0 code, not the final
  release tag. Default: 14 tools; legacy: 30 tools, `KITSUNE_SCRIPTING=0`; no benchmark allowlist.
- Playwright MCP 0.0.83: 23 tools under the benchmark's standard UI allowlist. ego lite 0.6.2.9:
  one `run_script` tool. Aside CLI/native MCP 1.26.1107.24 (app 1.0.1022.1): one `repl` tool.
- MiniWoB `33c3b4ddef8c6eb67c57a29663d844b1eda7e614`, seed 42. Tasks: `click-test-2`,
  `click-checkboxes`, `enter-text`, `choose-list`, `login-user`, `use-autocomplete`, `book-flight`.
  All planned attempts were scorable; no episode retries or replacements. ego failed `book-flight` r1.
- Kitsune made **zero Python calls**: this supports a smaller observed tool/prompt prefix,
  not a measured Python batching benefit. Across the full 14-cell Kitsune-only comparison,
  input fell 33.1%, but model requests rose 4.2% and MCP calls rose 2.3%.
- Kitsune/Playwright used fresh private headless Firefox. ego/Aside used installed warm browsers;
  engines, profiles and effective viewports were not equal. No isolation equivalence is claimed.
- Page-JS code was emitted in eight ego attempts and one Aside attempt, including successful
  attempts. These remain in the primary results; raw success is **not** strict UI-only compliance.
  On pairwise successful cells excluding page-JS use, ego/default input was 0.905 (6 cells),
  Aside/default 0.684 (13 cells). These smaller, selected subsets are not replacements for the chart.
- This small exhibition does not establish statistical superiority, approval safety, full-suite
  performance, or general speed/cost savings.

Source: `kitsune-benchmark/docs/results/2026-11-12-five-way-main-79eb955.md`, sections
“All-attempt totals” and “Matched efficiency, not success-blind totals”; benchmark execution
commit `d75bbff586890f0857e6d947189ad0895fd7edf2`. These historical figures are retained separately
and are not part of the current README chart.

## Earlier benchmark: v0.7.2

![Kitsune v0.7.2 vs Playwright MCP 0.0.83: kept the user's tab in front in 28 of 28 tasks vs 2 of 28; 0.56 to 0.97 times the cost on tasks both solved; Kitsune solved 96 of 98 tasks, Playwright MCP 98 of 98](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/benchmark.png)

Same tasks, same model (Claude Opus 5.5), same tool allowlist, one run per task:

- **Your tab stays in front.** While a user was in the browser, Kitsune v0.7.2 never moved their view in 28 tasks.
  Playwright MCP, which was not built to share a browser with a person, moved it in 26.
- **It costs less.** On the tasks both solved, Kitsune cost 0.56 to 0.97 times as much as Playwright MCP.
- **It solves about as many.** Kitsune solved 96 of 98 tasks, Playwright MCP 98. Kitsune's two misses are one
  task, "go back to the tab I was on", in two variants: the user's tab stayed in front, but the agent's own tab was
  still its current tab, and that is what the grader checks.

See the [v0.7.2 report](https://github.com/junyeong-nero/kitsune-benchmark/blob/main/docs/results/2026-11-04-opus-subscription-v0.7.2.md)
and the [full reports](https://github.com/junyeong-nero/kitsune-benchmark/tree/main/docs/results).
`scripts/benchmark_chart.py` draws the chart from the report's numbers.

## How is it different?

| If you want to… | Consider |
| --- | --- |
| Hand tasks to an agent in the Firefox-family browser you are using, with background tabs and per-tool approvals | **Kitsune** |
| Debug or profile pages in Firefox (debugger, profiler, deep protocol access) | [Mozilla Firefox DevTools MCP](https://github.com/mozilla/firefox-devtools-mcp), which attaches to a Firefox started with remote-debugging flags |
| Test across Chromium, Firefox and WebKit in browsers it launches | [Playwright MCP](https://github.com/microsoft/playwright-mcp); Kitsune uses the same core tools, so prompts carry over |
| Drive the Chrome you already use | A Chromium extension MCP such as [Browser MCP](https://github.com/BrowserMCP/mcp) |

Kitsune connects through an extension and a local bridge, so Firefox needs no startup flags.
Other Firefox extensions also reuse your logins; Kitsune focuses on sharing the browser with you
while the agent works. [Positioning](https://github.com/junyeong-nero/kitsune/blob/main/docs/positioning.md)
has the full comparison.
