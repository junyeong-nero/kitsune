# Minimal tools + Python

Set `KITSUNE_SCRIPTING=1` in the MCP server environment and restart the client.
This selects the minimal tool-set **and permits trusted local Python execution**.
It is not a sandbox: scripts have ordinary host file/network access. See
[execution and trust limits](python-api.md#python-scripting-through-mcp).
Unset the variable (or set `0`) and restart to return to the unchanged legacy list.
Existing users are not switched automatically.

## Direct tools

In `act` mode with `core` enabled, the list is 14 tools:

- `browser_tabs`, `browser_navigate`
- `browser_snapshot`, `browser_find`
- `browser_click`, `browser_type`, `browser_fill_form`, `browser_select_option`
- `browser_press_key`, `browser_wait_for`, `browser_take_screenshot`
- `browser_ask_user`, `browser_resume`
- `browser_run_python`

Use a direct tool for a simple action; use Python for loops, conditions, filtering
and less-frequent operations. `browser_run_python` supplies `browser` and the current
MCP-selected `tab`, so `tab.hover("#menu")` needs no index-to-ID conversion.
`help(tab.hover)` prints a method's signature/docstring when needed. The Python
tool's guide lists examples for the enabled capability groups and mode, regardless
of which direct tools `KITSUNE_PUBLISH` lists. `browser.tabs()` returns tab dicts,
not `Tab` objects; wrap a chosen page tab dict with `browser.tab(t["id"])`.

`read`/`draft` still omit disallowed direct tools and use `browser_tab_list` instead
of `browser_tabs`. `KITSUNE_CAPS` remains the browser authority limit; `all` still
excludes `storage` and `history`. `KITSUNE_PUBLISH`, if explicitly set, can narrow
which retained direct tools are listed. Scripting itself requires its separate
local-code opt-in and does not widen the supplied browser transport's grants.

For example, `KITSUNE_CAPS=core,browse,read` lets Python use ordinary page text,
queries, forms and scrolling in addition to core operations. Merely enabling
scripting does **not** add those groups. Additional authorized groups enable their
Python APIs without expanding the minimal MCP list. There is no search/call/publish
step or dynamic tool publication in this surface.

## Moving less-frequent tools to Python

| Former direct tool | Python or retained direct equivalent |
| --- | --- |
| `browser_close` | `tab.close()` or `browser_tabs action=close` |
| `browser_hover` | `tab.hover(target)` |
| `browser_drag` | `tab.drag(source, destination)` |
| `browser_drop` | `tab.drop(target, paths=..., data=...)` |
| `browser_file_upload` | `tab.upload_files(paths, target)` |
| `browser_handle_dialog` | `tab.handle_dialogs(accept, prompt_text)` |
| `browser_navigate_back` | `tab.back()` |
| `browser_resize` | `tab.resize_window(width, height)` |
| `browser_console_messages` | `tab.watch_console(); result = tab.console_logs()`; filter the list in Python |
| `browser_network_requests`, `browser_network_request` | `entries = tab.network.resources()`; filter/index that list in the same script. There are no response headers/bodies, just as before |
| `browser_evaluate` | `tab.evaluate(function, target)`; still requires the developer extension and existing permission |
| `browser_emulate_media` | Already an unsupported compatibility stub; no working feature is removed |
| `browser_run_code_unsafe` | Already an unsupported Playwright stub; use Python composition, not a Playwright `page` object |

Other groups also stay off this MCP list: their existing Python methods are in the
[API reference](python-api.md), such as `tab.query`, `tab.readability`,
`tab.computed_style`, `browser.windows`, `browser.cookies` and `browser.history`.
Keep the same required grants. Methods return the existing bridge/API results;
this is not a schema-compatible replacement for every old MCP result.

A call by an old tool name reports `tool_moved_to_python` (or its existing
capability/mode refusal if unauthorized). Scripts stop at approval and never replay
completed work. Use a retained direct action to ask for approval where possible;
for an approval-gated operation with no suitable retained direct tool, use the
legacy surface or a developer-written, user-mediated Python program. Automatic
script approval/resume is not part of this minimal version.

## Why these tools (#515)

Offline review of the existing [main `ad04bac` cohort](https://github.com/junyeong-nero/kitsune-benchmark/blob/52e455bd4387ca76fac905e220536b5ab350b809/docs/results/2026-11-08-main-ad04bac.md):
**167 Kitsune runs, 60 distinct task IDs, 1,272 agent calls**. Count unique assistant
`tool_use` IDs and matching results, not harness/setup calls. Task IDs are deduplicated
across variants/layouts/coexistence arms. No new model/browser benchmark was run.

| Tool | Calls | Runs using it / 167 | Distinct tasks / 60 |
| --- | ---: | ---: | ---: |
| snapshot | 410 | 166 | 59 |
| tabs | 333 | 133 | 46 |
| click | 245 | 162 | 59 |
| type | 71 | 67 | 25 |
| fill_form | 64 | 63 | 19 |
| find | 52 | 27 | 14 |
| take_screenshot | 27 | 10 | 5 |
| press_key | 26 | 6 | 4 |
| navigate | 20 | 11 | 6 |
| select_option | 9 | 6 | 3 |
| wait_for | 8 | 5 | 3 |
| hover | 7 | 1 | 1 |

Keep form-fill because it is broadly used, not just repeated in one task. Keep
select/key/wait as cheap common controls, screenshots for visual observations,
and help/resume regardless of frequency. Hover's seven calls all came from one
task; it and the other specialized operations have short Python replacements.
This keeps 1,265 of the 1,272 observed calls on direct tools—not proof that tasks
will behave identically after the change.

There are no missing results or known unadvertised calls in the collected data.
Native exposure metadata exists for 157 runs; the 10 external runs (80 calls) lack
that file, so do not treat them as exposure evidence. Eleven other advertised
names had zero calls in the 157 metadata-backed runs; that is not evidence they
are useless. Evaluate and the unsafe-code stub were not advertised in that cohort.
This mixed, repeated benchmark sample is **not production usage frequency**.

Source artifacts (private traces remain private):
`kitsune-benchmark/artifacts/opus-subscription-main-ad04bac/suite-log.jsonl`,
its referenced suite summaries and `events.jsonl`, and `per-tool.txt`/`per-tool.json`.
The suite-log SHA-256 is
`64fa0ff87e6c1a95b5b9efeb6a3c3ca72ba688b769dbe4d3a6611191a0eea05d`.
The existing `kitsune_benchmark.tool_usage.calls_from_events` collector was reused
for deduplicating calls; counts agree with the existing per-tool report.

Actual `tools/list` size in `act` mode with `core` enabled (compact JSON characters,
including the Python tool's short API guide):

| Surface | Tools | Definitions | Server instructions | Combined |
| --- | ---: | ---: | ---: | ---: |
| Legacy, scripting off | 27 | 21,813 | 537 | 22,350 |
| Minimal, scripting on | 14 | 12,655 | 564 | 13,219 |

That is about 42% fewer definition characters, or 41% including instructions.
These are **not model tokens**. Generated Python, on-demand help and task results
are additional costs; no end-to-end token saving has been measured. The local
checks cover functionality, permission refusals and lifecycle, not a controlled
efficiency claim.
