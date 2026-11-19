# Benchmark and comparison

## Four-way, 12 tasks (2026-11-19)

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

## Earlier results

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
