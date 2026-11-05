# Benchmark and comparison

## Benchmark

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
| Hand tasks to an agent in the Firefox-family browser you are using, with background tabs and read/draft/act modes | **Kitsune** |
| Debug or profile pages in Firefox (debugger, profiler, deep protocol access) | [Mozilla Firefox DevTools MCP](https://github.com/mozilla/firefox-devtools-mcp), which attaches to a Firefox started with remote-debugging flags |
| Test across Chromium, Firefox and WebKit in browsers it launches | [Playwright MCP](https://github.com/microsoft/playwright-mcp); Kitsune uses the same core tools, so prompts carry over |
| Drive the Chrome you already use | A Chromium extension MCP such as [Browser MCP](https://github.com/BrowserMCP/mcp) |

Kitsune connects through an extension and a local bridge, so Firefox needs no startup flags.
Other Firefox extensions also reuse your logins; Kitsune focuses on sharing the browser with you
while the agent works. [Positioning](https://github.com/junyeong-nero/kitsune/blob/main/docs/positioning.md)
has the full comparison.
