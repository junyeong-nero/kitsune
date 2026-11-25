# Benchmark and comparison

## WebArena-Pro four-way, 26 tasks (2026-11-20)

Aside, EgoLite, Playwright MCP and Kitsune on 26 tasks (19 test, 7 dev) from
[WebArena-Pro](https://arxiv.org/abs/2611.17955) (NeurIPS 2026 E&D): multi-step work on 8 self-hosted apps, such as
booking the cheapest weekend trip, registering for a course that fits a schedule or replying to every unread email.
Two agents, Claude Code (`claude-opus-5-5`) and Codex (`gpt-6.1-sol`), one attempt per task: 208 attempts with a
600 s budget each. An attempt passes only with a full score from the task's backend-state and answer checks.

Kitsune's Claude Code row is v0.8.3: its 26 attempts were run again alone on 2026-11-25, on the same model, tasks,
sites and grading, with an API key instead of a subscription. The other seven rows are from 2026-11-20, when Kitsune
itself measured 13/26, 283k input and 14.0 tool calls per attempt under Claude Code. Kitsune's Codex row is that
earlier build. The v0.8.3 run used commit `0a8ada9`; the release adds only the `kitsune setup` command on top, with
no change to the server, tools or extension.

![Four-way benchmark, 26 WebArena-Pro tasks, 208 attempts. Passed, Claude Code / Codex: Kitsune 13/26 and 12/26; Playwright MCP 13/26 and 12/26; Aside 12/26 and 12/26; EgoLite 12/26 and 12/26. Mean input tokens per attempt: Kitsune 201k / 414k; Aside 463k / 512k; EgoLite 360k / 622k; Playwright MCP 660k / 809k. Mean tool calls: Kitsune 12.1 / 15.8; Aside 16.1 / 15.6; EgoLite 12.8 / 17.5; Playwright MCP 29.8 / 35.1.](https://raw.githubusercontent.com/junyeong-nero/kitsune/main/assets/benchmark-webarena-pro.svg)

| Arm | Passed (Claude / Codex) | Mean score | Tool calls / attempt | Input / attempt, incl. cache | Median s |
| --- | ---: | ---: | ---: | ---: | ---: |
| **Kitsune** | 13/26 / 12/26 | 0.64 / 0.57 | **12.1** / 15.8 | **201k / 414k** | 45.5† / 70.3 |
| Playwright MCP | 13/26 / 12/26 | 0.64 / 0.57 | 29.8 / 35.1 | 660k / 809k | **59.9** / 64.0 |
| Aside (CLI REPL) | 12/26 / 12/26 | 0.61 / 0.57 | 16.1 / **15.6** | 463k / 512k | 67.7 / **58.6** |
| EgoLite (CLI) | 12/26 / 12/26 | 0.61 / 0.59 | 12.8 / 17.5 | 360k / 622k | 63.9 / 68.5 |

† Measured on another day and with API-key billing, so this time is not compared with the other arms'. On
2026-11-20 Kitsune's median was 59.9 s.

By split: of the 19 test tasks, Kitsune and Playwright MCP passed 7 under Claude Code and Aside and EgoLite 6; every
arm passed 6 under Codex. Every arm passed 6 of the 7 dev tasks under both agents.

Totals on the 12 tasks every arm passed under each agent. They are not the same 12 under both. The percentages are
ratios of those totals to Playwright MCP's, not reductions:

| Arm | Input, incl. cache (Claude / Codex) | vs Playwright MCP | Tool calls (Claude / Codex) |
| --- | ---: | ---: | ---: |
| **Kitsune** | **1.10M / 2.76M** | 40% / 87% | **90** / 135 |
| Aside (CLI REPL) | 2.76M / 3.13M | 99% / 98% | 140 / **128** |
| EgoLite (CLI) | 2.14M / 3.74M | 77% / 117% | 94 / 133 |
| Playwright MCP | 2.78M / 3.19M | 100% / 100% | 201 / 258 |

- **Success was tied.** Under each agent all four arms passed the same 12 tasks. Under Claude Code, Kitsune and
  Playwright MCP passed one more: its answer check needs the flight number, which their answers included and the
  Aside and EgoLite answers left out.
- **Input:** Kitsune used the least under both agents, but by how much depends on the agent and on which attempts
  count. Per attempt it used 31% of Playwright MCP's input under Claude Code and 51% under Codex. On the tasks every
  arm passed, where a short failed attempt cannot look cheap, it used 40% and 87%. From the 2026-11-20 build to
  v0.8.3, Kitsune's input under Claude Code fell on the tasks it failed and stayed the same on the tasks it passed.
- **Tool calls:** about half of Playwright MCP's under both agents. Per attempt Kitsune made the fewest under Claude
  Code and Aside under Codex.
- **Time:** no consistent difference. Under Codex, Kitsune had the highest median.
- **Kitsune's problems on 2026-11-20:** navigation to image-heavy pages timed out 7 times under Claude Code and 3 times under
  Codex, which stopped the script that made it ([#561](https://github.com/junyeong-nero/kitsune/issues/561)). Under
  Codex, 9 scripts ran into the 16 KB output limit. In one Claude Code task the agent asked which posts to save
  instead of saving them.

Limits:

- One attempt per task and agent; a one-task difference is within noise.
- Kitsune's Claude Code row is from a later day and build than the rows it is compared with, and was billed through
  an API key, not a subscription. The model, tasks, sites and grading are the same.
- Grading has no LLM judge or UI evidence, so a rubric the backend check cannot prove fails. It is stricter than the
  paper's grading, and these scores are not comparable to its 62.3%. Of the tasks no arm passed, the failing rubric
  fell through to that judge in 11 of 13 under Claude Code and 12 of 14 under Codex.
- Browser environments differ: Aside and EgoLite drove the operator's profiles; Playwright MCP and Kitsune ran an
  isolated headless Firefox.
- The sites ran in a GitHub codespace over forwarded ports.
- For CLI arms one tool call is one script that can batch actions. Input counts are usage counters, not billed cost.
- Four Claude Code attempts, one task that timed out in every arm, have no usage receipt. Their token use is unknown,
  not zero, so Claude Code's input means and totals cover 25 attempts per arm.

Source: the [kitsune-benchmark report](https://github.com/junyeong-nero/kitsune-benchmark/blob/b9e1fa8/docs/results/2026-11-20-webarena-pro-four-way.md)
and its [per-attempt CSV](https://github.com/junyeong-nero/kitsune-benchmark/blob/b9e1fa8/docs/results/2026-11-20-webarena-pro-four-way.csv),
which has every attempt's status, score, time, tool calls, tool errors and token counts. Kitsune's Claude Code row:
the [v0.8.3 check](https://github.com/junyeong-nero/kitsune-benchmark/blob/480b818/docs/results/2026-11-25-kitsune-main-0a8ada9-claude-api.md)
and its [CSV](https://github.com/junyeong-nero/kitsune-benchmark/blob/480b818/docs/results/2026-11-25-kitsune-main-0a8ada9-claude-api.csv).

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
  than Aside ([report](https://github.com/junyeong-nero/kitsune-benchmark/blob/4de2f72/docs/results/2026-11-12-five-way-main-79eb955.md)).
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
