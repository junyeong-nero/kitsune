# Development

## Setup from a checkout

```bash
git clone https://github.com/junyeong-nero/kitsune ~/workspace/kitsune
cd ~/workspace/kitsune
uv sync
```

Register the checkout instead of the PyPI package so the client runs your
working tree directly:

```bash
claude mcp add --scope user kitsune -- uv run --directory ~/workspace/kitsune kitsune
```

In JSON or TOML configs, use `"command": "uv"` with
`["run", "--directory", "/Users/you/workspace/kitsune", "kitsune"]`. Load the
extension from the checkout's `extension/manifest.json` (that is the developer
build). To try the signed variant locally, build it and load
`build/extension-signed/manifest.json` the same way:

```bash
uv run python scripts/build_extension.py signed
```

To test the installed-package path of a branch, build both packages and run the server
from their wheels; rebuild after you edit files:

```bash
uv build --all-packages
uvx --from dist/kitsune_browser_mcp-<version>-py3-none-any.whl \
    --with dist/kitsune_browser-<version>-py3-none-any.whl kitsune
```

(`uvx --from ./packages/kitsune-browser-mcp kitsune` instead runs the working tree's
code, which uv installs editable.)

## Tests

Run the tests from the repository root:

```bash
uv run python -m unittest discover -s tests -q
for test in tests/*.cjs; do node "$test"; done
```

The Python regression suite covers the MCP tool surface, snapshots, navigation
waits, and bridge lifecycle; the Node harnesses cover content-script behavior
such as refs, form filling, and password redaction. For an end-to-end check, load the extension in a
supported browser and follow [Try it](try-it.md); unit tests alone do not verify
your live browser connection. File reproducible bugs in
[GitHub Issues](https://github.com/junyeong-nero/kitsune/issues), including the
browser version, bridge `build` from `browser_status`, tool calls, and expected
versus actual behavior. Use a minimal public or local page and redact secrets.

Tool definitions are resent with every model request, so their size is tracked:
`uv run python scripts/tool_schema_size.py` reports the compact JSON size of each
`core` tool in two views: the full definition, and the definition with its description
cut to the first paragraph, as some clients (Claude Code `--bare`) send it. A test fails
until `--update` records a change in `tests/tool_schema_sizes.json`. The same test fails
when the `core` total grows past `TOTAL_BUDGETS` or `FIRST_PARAGRAPH_BUDGETS`, or any
single tool past `TOOL_BUDGET`, or the server instructions of a mode past
`INSTRUCTIONS_BUDGETS`; raise a budget only deliberately. The `core` tools are
Playwright MCP's, published verbatim, and `tests/test_playwright_tools.py` checks the
legacy (`KITSUNE_SCRIPTING=0`) `tools/list` against `src/kitsune/playwright_tools.json`. The Kitsune-owned tools'
published input schemas omit the `title` keywords pydantic generates (`KitsuneMCP.list_tools`), and
`tests/test_tool_schema_titles.py` checks that they still accept and reject the same
arguments. They also publish an optional parameter whose default is null as its plain
type instead of `anyOf: [type, null]`, and omit `null` and `""` defaults. Calls are
still validated against the pydantic models, so an explicit `null` still works and
behaves as omitted; `tests/test_tool_schema_optionals.py` checks this for every tool.
Every client sends input schemas in full, so parameters are documented there
(`Annotated[..., Field(description=...)]`), not in `Args:` blocks. A tool's first
paragraph says what it does and what a caller must know. The Kitsune-owned action tools
share one contract (ref, element, wait, expect, confirm_token, first; confirm_token is the
approval fallback for clients without elicitation), documented once in
`browser_check`'s parameter descriptions; the other action tools say in their first
paragraph that `ok` means dispatched, name `expect` and point to `browser_check` rather
than repeating it. `tab_id` is described wherever it is accepted.
`tests/test_tool_first_paragraph.py` checks these rules for every Kitsune-owned tool.

### Real-browser suites

The opt-in real-browser suites (`tests/test_real_browser*.py`) run against a real
Firefox, Zen or LibreWolf. They cover action races, tab unloads and reconnects
(`test_real_browser.py`), tabs opened by `target=_blank` clicks with popups allowed
and with the default popup blocker (`test_real_browser_opened_tabs.py`), every
read tool on empty 404 and 500 pages (`test_real_browser_error_pages.py`), and
which form or button an action picks on a page with repeated controls
(`test_real_browser_action_targets.py`). They are skipped unless `KITSUNE_E2E_BROWSER`
names the browser binary. A full run takes several minutes, depending on the host
and browser; run one suite when iterating:

```bash
KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen \
  uv run python -m unittest discover -s tests -p 'test_real_browser*.py' -v
```

Use `-p test_real_browser_error_pages.py` (or another file name) to run one suite.
Each test class starts a headless browser on a new temporary profile and HOME, plus
its own bridge and fixture site on spare ports, and kills them afterwards. Your
browser profiles and running bridge are not touched. The suites run a copy of the
extension whose bridge port is rewritten to the test bridge. The browser does not
start if any script in the copy still names port 47652, or if the test bridge would
use that port. The shared harness, `tests/real_browser_harness.py`, documents how to
add a suite, including how to choose the profile prefs (browser defaults or
popups allowed).

To keep evidence from a failing run, set `KITSUNE_E2E_ARTIFACTS` to a directory:

```bash
KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen \
  KITSUNE_E2E_ARTIFACTS=/tmp/kitsune-e2e-artifacts \
  uv run python -m unittest discover -s tests -p 'test_real_browser*.py' -v
```

Each failing test, or failing class setup, writes one directory under
`kitsune-e2e-<UTC time>-<pid>/<module.Class>/` and prints its path to stderr.
`timeline.jsonl` puts the harness's bridge requests, the fixture server's request
events and the browser's top-level navigation events (load start/stop, location
changes, tab open/close) on one wall clock, so a slow page load can be told apart from
a slow fixture reply. For each fixture request it records when the request was
received and then either `request-handled` (the handler returned and the server
flushed its reply) or `request-failed` with the exception type (for example
`BrokenPipeError` after the browser dropped the connection; the error still reaches
the server). Both are server-side only and do not show that the browser received the
reply. Only bridge requests sent through the harness's `call()` are in the timeline;
`open_tab` (`/api/new-tab`) is, but suites that send `harness._request` directly
(`test_real_browser.py`, `test_real_browser_protected_tabs.py`,
`test_real_browser_submit_form.py`, `test_real_browser_connection_diagnostics.py`)
appear only in `bridge.log`. Harness and fixture events share one buffer of 4000
events per test process; `summary.json` reports how many events recorded since the
class started were evicted (`droppedSinceClassStart`, with `complete: false` when
any were) and, separately, all evictions in the process (`droppedInProcess`).
`browser.log` and `bridge.log` hold the last 256 KiB of the owned browser's and
bridge's output, `resources.json` the load average and ps samples of the test runner,
the bridge and the browser's process group, and `summary.json` what was collected. Passing classes write nothing; a class
keeps at most five failures; nothing under the directory is ever deleted by the
harness. The timeline and summaries hold no headers, request bodies or page contents,
and URLs in them keep only a loopback origin and path. The two log tails are copied as
the owned processes wrote them, with the bridge token, Authorization, Bearer and
`token=` values redacted; with the variable set, the owned bridge writes its log
unbuffered so that it is current when a test fails. Without the variable the suites
run exactly as before.

## Releasing

To publish a new version to PyPI, see
[Releasing](https://github.com/junyeong-nero/kitsune/blob/main/docs/releasing.md).

## Layout

```
extension/          WebExtension (manifest v2, Gecko)
packages/kitsune-browser-mcp/  The MCP server's PyPI package: pins kitsune-browser[mcp], adds the commands
src/kitsune/        The kitsune-browser package (the repository root's pyproject.toml)
  server.py         MCP server: registers the tools on FastMCP, within KITSUNE_CAPS and KITSUNE_MODE
  tools.py          The agent tools (Kitsune's own) without MCP code; playwright_tools.py has the core group
  browser.py        Python API (kitsune.connect, Browser, Tab); errors.py has its exceptions
  bridge_client.py  Bridge requests and connection errors; starts the bridge on demand
  bridge.py         Bridge daemon (HTTP API + WebSocket)
  workflows/        Named workflows for browser_workflow
docs/               Documentation site (mkdocs.yml), API reference, scraping playbooks
```

## Credits

Kitsune started as a fork of [ZenLink](https://github.com/JayQuan-McCleary/ZenLink)
and [ZenLink-MCP](https://github.com/JayQuan-McCleary/ZenLink-MCP) by Jay-Quan
(MIT). Changes since the fork: token auth, Host and origin checks, no CORS,
on-demand shared daemon, one package, `browser_*` tool names, `~/.kitsune` data
directory, macOS support, `mcp<2` pin.

## License

MIT. See [LICENSE](https://github.com/junyeong-nero/kitsune/blob/main/LICENSE).
