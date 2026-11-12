# Code-first tools + Python

The default on `main` is six tools instead of 0.8.0's fourteen; this change is not
part of the 0.8.0 release. A smaller catalog does not guarantee lower end-to-end
token use or latency. The API and browser engine are unchanged; ordinary reads,
actions and waits use Python, with direct tools for user control, images and recovery.
This tool-set **permits trusted local Python execution**:
it is not a sandbox, and scripts have ordinary host file/network access. See
[execution and trust limits](python-api.md#python-scripting-through-mcp).
Set `KITSUNE_SCRIPTING=0` in the MCP server environment and restart the client to
return to the legacy direct tool list without `browser_run_python`. Unset or `1`
selects this tool-set; any other value fails at startup.

## Direct tools

The list is six tools:

- `browser_run_python`: ordinary reads, inputs, waits, conditions and filtering
- `browser_tabs`: selection, handoff and tab lifecycle
- `browser_take_screenshot`: return images to the model
- `browser_ask_user`, `browser_resume`: human help and direct-call continuation
- `browser_tools_call`: inspect/execute one direct operation for approval or recovery

Use Python for simple actions too: `result = tab.scroll('down', 700)`.
Group only actions whose targets are already known, then return the observation
needed for the next decision, e.g. `result = tab.snapshot(text=True)`. Do not guess
refs or force an unknown workflow into one script. Read method-level help rather
than the whole API when one signature is missing. `browser_run_python` supplies `browser` and the current
MCP-selected `tab`, so `tab.hover("#menu")` needs no index-to-ID conversion.
`help(tab.hover)` prints a method's signature/docstring when needed. The Python
tool's guide lists examples of the calls agents got wrong. `browser.tabs()` returns tab dicts,
not `Tab` objects; wrap a chosen page tab dict with `browser.tab(t["id"])`.

Every Python method is available (#525): page text, queries, forms, scrolling,
cookies and history included. Approvals and the user-tab protection apply to its
browser requests as to direct calls. There is no search/publish step or dynamic
tool publication. The recovery dispatcher is not the ordinary browsing loop.

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
Methods return the existing bridge/API results;
this is not a schema-compatible replacement for every old MCP result.

Old direct tool names remain callable for compatibility, with unchanged validation
and approval handling; `tools/list` is discovery, not an authorization boundary.
Clients that require advertised names can use `browser_tools_call`.

A tool the user requires approval for is asked about mid-script and its request sent
again once allowed ([tool approvals](your-tabs.md#tools-that-need-your-approval)).
Other approval stops end the script and never replay completed work. A held operation
reports `failed_operation.recovery_tool`: call `browser_tools_call` with that name and
**omit arguments** to inspect its schema without acting. Then supply an argument object
to execute only the held operation through the existing direct approval path. An explicit
`{}` executes a no-argument tool; it is not a schema lookup. Handoffs use `browser_tabs`.
After recovery, inspect the page and run only remaining work in a new script.
Non-eliciting clients retain the direct tool's explicit confirm-token fallback; scripts
still cannot submit tokens. The script as a whole is never checkpointed or resumed.

## Historical fourteen-tool selection (#515)

The following historical evidence describes 0.8.0's fourteen-tool surface, not the
six-tool default. It explains the previous selection, not the performance of code-first.

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

Actual `tools/list` size in 0.8.0 (compact JSON characters, including the Python tool's
short API guide):

| Surface | Tools | Definitions | Server instructions | Combined |
| --- | ---: | ---: | ---: | ---: |
| Legacy (`KITSUNE_SCRIPTING=0`): 27 core + 3 gateway | 30 | 24,032 | 537 | 24,569 |
| Legacy core tools alone (before 0.8.0's default listing) | 27 | 21,747 | 537 | 22,284 |
| Minimal (default) | 14 | 9,688 | 564 | 10,252 |

The minimal list has about 60% fewer definition characters than the 0.8.0 legacy list,
and 55% fewer than the core tools alone. Since 0.8.0 the legacy list always carries the
gateway tools, because every other group's tools are available and reachable through them.
On this surface only, the retained Playwright tools publish shorter parameter
descriptions and no `$schema` (#521); their names, types, enums, required
parameters and defaults are unchanged, and the legacy list stays verbatim. Before
that change the minimal definitions were 12,589 characters.
These are **not model tokens**. Generated Python, on-demand help and task results
are additional costs; this historical schema-size comparison did not measure
end-to-end token savings. Its local checks cover functionality, permission refusals
and lifecycle, not a controlled efficiency claim.
