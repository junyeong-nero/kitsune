# Benchmark and comparison

## WebArena-Pro four-way, 26 tasks (2026-11-20)

Aside, EgoLite, Playwright MCP and Kitsune on 26 tasks from [WebArena-Pro](https://arxiv.org/abs/2611.17955)
(NeurIPS 2026 E&D): multi-step work on 8 self-hosted apps, such as booking the cheapest weekend trip, registering
for a course that fits a schedule or replying to every unread email. Claude Code (`claude-opus-5-5`), one attempt
per task, 104 attempts, a 600 s budget each. An attempt passes only with a full score from the task's backend-state
and answer checks.

![Four-way benchmark, 26 WebArena-Pro tasks, Claude Code, 104 attempts. Passed: Kitsune 13/26; Playwright MCP 13/26; Aside 12/26; EgoLite 12/26. Mean input tokens per attempt: Kitsune 283k; EgoLite 360k; Aside 463k; Playwright MCP 660k. Mean tool calls: EgoLite 12.8; Kitsune 14.0; Aside 16.1; Playwright MCP 29.8.](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/benchmark-webarena-pro.svg)

| Arm | Passed | Mean score | Tool calls / attempt | Input / attempt, incl. cache | Median s | List-price cost (sum) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Kitsune** | 13/26 | 0.63 | 14.0 | **283k** | 60 | **$6.7** |
| Playwright MCP | 13/26 | 0.64 | 29.8 | 660k | 60 | $12.4 |
| Aside (CLI REPL) | 12/26 | 0.61 | 16.1 | 463k | 68 | $10.3 |
| EgoLite (CLI) | 12/26 | 0.61 | **12.8** | 360k | 64 | $9.3 |

Totals on the 12 tasks every arm passed:

| Arm | Input | Tool calls |
| --- | ---: | ---: |
| **Kitsune** | **1.08M** | **91** |
| EgoLite (CLI) | 2.14M | 94 |
| Aside (CLI REPL) | 2.76M | 140 |
| Playwright MCP | 2.78M | 201 |

- **Success was tied.** All four arms passed the same 12 tasks and missed the same 13. One task split them:
  its answer check needs the flight number, which the Kitsune and Playwright MCP attempts reported and the Aside
  and EgoLite attempts left out.
- **Cost:** Kitsune used the least input. On the tasks every arm passed it used 39% of Playwright MCP's input with
  91 tool calls to its 201. EgoLite made slightly fewer tool calls per attempt than Kitsune.
- **Kitsune's problems:** navigation to image-heavy pages timed out 7 times, which stopped the script that made it
  ([#561](https://github.com/junyeong-nero/kitsune/issues/561)). In one task the agent asked which posts to save
  instead of saving them.

Limits:

- One attempt per task; a one-task difference is within noise.
- Grading has no LLM judge or UI evidence, so a rubric the backend check cannot prove fails. It is stricter than the
  paper's grading, and these scores are not comparable to its 62.3%. In 11 of the 13 shared misses the failing
  rubric fell through to that judge.
- Browser environments differ: Aside and EgoLite drove the operator's profiles; Playwright MCP and Kitsune ran an
  isolated headless Firefox.
- The sites ran in a GitHub codespace behind a port-forward tunnel, which slows heavy page loads.
- For CLI arms one tool call is one script that can batch actions. Cost is Claude Code's list-price estimate, not
  billed cost.

Source: [kitsune-benchmark report](https://github.com/junyeong-nero/kitsune-benchmark/blob/main/docs/results/2026-11-20-webarena-pro-four-way.md).

## Earlier results

### Four-way, 12 tasks (2026-11-19)

Aside, EgoLite, Playwright MCP and Kitsune on 12 original local web tasks (forms, pagination, a native
`confirm()` dialog, delayed content, shadow DOM, an iframe, a wizard) under two agents: Claude Code
(`claude-opus-5-5`) and Codex (`gpt-6.1-sol`). One attempt per task, 96 attempts. An attempt passes only if
the fixture server received exactly one correct submission.

![Four-way benchmark, 12 local tasks, 96 attempts. Passed, Claude Code / Codex: Kitsune 11/12 and 10/12; Aside 12/12 and 12/12; EgoLite 12/12 and 11/12; Playwright MCP 12/12 and 10/12. Mean input tokens per attempt: Kitsune 31k / 66k; Aside 53k / 79k; EgoLite 71k / 76k; Playwright MCP 65k / 93k. Mean tool calls: Kitsune 3.2 / 2.9; Aside 4.7 / 4.8; EgoLite 4.0 / 3.5; Playwright MCP 7.1 / 5.9.](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/benchmark-four-way.svg)

| Arm | Passed (Claude / Codex) | Tool calls / attempt | Input / attempt, incl. cache | Median s |
| --- | ---: | ---: | ---: | ---: |
| **Kitsune** | 11/12 / 10/12 | **3.2 / 2.9** | **31k / 66k** | **15.6** / 24.1 |
| Aside (CLI REPL) | 12/12 / 12/12 | 4.7 / 4.8 | 53k / 79k | 24.3 / 35.5 |
| EgoLite (CLI) | 12/12 / 11/12 | 4.0 / 3.5 | 71k / 76k | 17.0 / **22.9** |
| Playwright MCP | 12/12 / 10/12 | 7.1 / 5.9 | 65k / 93k | 17.6 / 32.4 |

- **Cost:** Kitsune made the fewest tool calls and used the least input under both agents. Under Claude Code it
  used 47% of Playwright MCP's input at 79% of its cost. The order holds on the tasks every arm solved.
- **Kitsune's miss:** the native `confirm()` task, under both agents. The open dialog blocked the click until it
  timed out, and `browser_handle_dialog` set a policy for later dialogs instead of accepting the open one.
- **Other tools:** EgoLite snapshots do not show checkbox state, so Codex saved the wrong preferences. Aside's
  one-shot REPL calls lose snapshot refs (22 `RefStaleError`s). Playwright MCP had no tool errors; its two Codex
  misses were Codex stalls.

Limits:

- One attempt per task and agent; a one-task difference is within noise.
- Playwright MCP ran an isolated headless Firefox; the other three drove the operator's browser.
- For CLI arms one tool call is one script that can batch actions. Input tokens are not billed cost.
- The tasks are small and deterministic: no long navigation, real logins or visual reasoning.

Source: [kitsune-benchmark report](https://github.com/junyeong-nero/kitsune-benchmark/blob/main/docs/results/2026-11-19-four-way-12-task.md).

### Before 2026-11-19

- **Four-way comparison (2026-11-13):** 23 conditions × 2 repeats under Codex `gpt-6.1-sol`; every arm
  completed 46/46. Six-tool Kitsune used 2.9% less input than 14-tool Kitsune, 3.3% less than Aside and 26.3%
  less than EgoLite, but its adjusted elapsed time was 4.8% and 6.6% higher than 14-tool Kitsune and Aside.
  Aside used less input in 36/46 paired cells.
- **Five-way MiniWoB (2026-11-12):** on 13 shared successful cells, Kitsune's then-default
  14 tools used 34.1% less input than Playwright MCP, but 12.4% more than ego lite and 42.6% more
  than Aside ([report](https://github.com/junyeong-nero/kitsune-benchmark/blob/main/docs/results/2026-11-12-five-way-main-79eb955.md)).
- **v0.7.2 vs Playwright MCP:** Kitsune kept the user's tab in front in 28/28 tasks (Playwright
  MCP 2/28), cost 0.56–0.97× on tasks both solved, and solved 96/98 tasks (Playwright MCP 98/98)
  ([report](https://github.com/junyeong-nero/kitsune-benchmark/blob/main/docs/results/2026-11-04-opus-subscription-v0.7.2.md)).

All reports: [kitsune-benchmark](https://github.com/junyeong-nero/kitsune-benchmark/tree/main/docs/results).

## How is it different?

| If you want to… | Consider |
| --- | --- |
| Hand tasks to an agent in the Firefox-family browser you use, with background tabs and approvals | **Kitsune** |
| Debug or profile pages in Firefox | [Firefox DevTools MCP](https://github.com/mozilla/firefox-devtools-mcp) |
| Test across Chromium, Firefox and WebKit in browsers it launches | [Playwright MCP](https://github.com/microsoft/playwright-mcp) |
| Drive the Chrome you already use | A Chromium extension MCP such as [Browser MCP](https://github.com/BrowserMCP/mcp) |
