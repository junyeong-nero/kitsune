# Python API

The `kitsune` package drives the browser you already have open from Python, through
the same extension and bridge as the MCP server. The two have different jobs:

- **The MCP server** gives an agent a small, curated tool surface.
- **The Python API** reaches every capability of the extension and bridge, for
  scripts and for your own agent frameworks, `storage` and `history` included.

Both are clients of one shared bridge, and the bridge's
[shared-browser rules](api-reference.md#shared-browser) hold for both: tabs open in
the background, writes wait while you are using a tab, your own tabs close only with
your approval, and sensitive fields are redacted. Ask-before-act, when it is on,
holds the same actions.

```bash
pip install kitsune-browser      # or: uv add kitsune-browser
```

kitsune-browser is the code; the MCP server's package, kitsune-browser-mcp, installs it
with the `mcp` extra. Keep a script on the version your MCP clients run, since they share
one bridge ([Connecting](#connecting)). It needs the Kitsune extension installed in
Firefox, Zen or LibreWolf ([Installation](installation.md)).

```python
import kitsune

browser = kitsune.connect()                        # starts the bridge if none runs
tab = browser.new_tab("https://news.ycombinator.com")   # a background tab you don't see
for link in tab.query(".titleline > a", fields=["text", "href"], limit=3):
    print(link["text"], link["href"])
tab.close()
```

## Connecting

`kitsune.connect()` starts the shared bridge if none runs, waits for the extension
to reconnect to a bridge it started, and raises `ConnectionFailed` if the extension
is not connected. It returns a `Browser`.

| Argument | Default | Effect |
| --- | --- | --- |
| `user_tabs` | unset (`read-only`) | `writable` allows this client to write user tabs. See [User-tab setting](your-tabs.md#protecting-your-tabs) |
| `confirm` | `None` | A function that decides on actions ask-before-act holds; see [Ask-before-act](#ask-before-act) |
| `replace_bridge` | `False` | `True` replaces a running bridge of another build |

`KITSUNE_HTTP_PORT`, `KITSUNE_TOKEN` and `KITSUNE_HOME` choose the bridge, as for the
MCP server ([Configuration](configuration.md)).

A script and your MCP clients share the bridge, so they should run the same Kitsune
version from the same source. A bridge of another build raises `ConnectionFailed`
with code `bridge_version_mismatch` and keeps running. The MCP server would replace
it, but replacing a bridge drops its clients' keep-alives, intercepts and resume
checkpoints, so the Python API does that only with `replace_bridge=True` (or after
`kitsune-bridge stop`).

## Reusing a host's client

An application that already owns a `BridgeClient` can wrap it without creating
another connection or changing its authority:

```python
from kitsune import Browser

browser = Browser(existing_client)  # host code: the same client used by its direct tools
```

This keeps that client's owner/client IDs, live handoff session and user-tab setting. Creating another `Browser(existing_client)` does not
create another browser session. Pass explicit tab IDs from `browser.tabs()` or
`new_tab()`; wrapping a client does not select a global current tab.

Do not call `connect()` to attach to an MCP session: it creates a separate developer
client of its own. `Browser` itself neither limits Python's
host access nor prevents code from modifying its client. It is an API wrapper, not a
script sandbox. A scripting host owns the execution boundary and approval channel;
an agent-supplied `confirm=lambda _: True` is not user approval. The
[MCP scripting tool](#python-scripting-through-mcp) uses a host-mediated transport
rather than giving the worker the actual `BridgeClient`.

## Python scripting through MCP

The default [code-first tool-set](minimal-tools.md) on `main` uses
`browser_run_python(code, timeout=30)` for ordinary reads, actions and waits; `KITSUNE_SCRIPTING=0` in the MCP server
environment selects the legacy direct tools without it.
It permits ordinary local Python execution: **not a sandbox**, including
host files, environment, imports, subprocesses and network access. Set
`KITSUNE_SCRIPTING=0` for a client you do not trust with local code execution.
Approvals and the user-tab protection apply to the supplied browser transport, not
to arbitrary Python or independent connections made by the code. No new runtime installation is needed: it uses the
MCP server's Python interpreter.

```python
# code passed to browser_run_python, after opening/selecting a tab with direct tools
for target, value in {"#name": "Ada", "#note": "Please review."}.items():
    tab.fill(target, value)
result = tab.snapshot()
```

- `browser` is the Python API, forwarding each request through the MCP host's
  existing client/session. **Do not call `kitsune.connect()`** to reuse that session.
  Every method is available (#525).
- `tab` is the current MCP selection at invocation start, or `None` before selection.
  Use `browser.new_tab(url)` or `browser.tab(id)` for other tabs. `browser.tabs()`
  returns `list[dict]` with `id`, `url`, `title`, etc., not `Tab` objects: for a chosen
  page tab dict `t`, use `browser.tab(t["id"]).snapshot()`. These are actual tab IDs,
  not the indexes of the legacy direct tab list. Scripts do not change the MCP selection.
- Every invocation has fresh Python variables and a temporary working directory,
  removed on exit. Browser tabs persist. Ordinary synchronous Python is supported;
  there is no persistent REPL or top-level `await`.
- Use `print(...)` or assign JSON-compatible `result`. The response contains
  `output`, optional `result` and a short `operations` list. Intermediate page
  results are not automatically returned to the model. Use direct screenshot tools
  for image content; Python screenshot bytes are not JSON results.
- Limits: 64,000 UTF-8 bytes of code; 16,000 bytes shared by printed output and the
  selected result; 100 mediated API calls; a 30-second default deadline, at most
  120 seconds. Internal browser replies are capped at 2 MiB. Operation metadata and
  a bounded failed-operation summary accompany the output.
- A failed API call stops the invocation even if Python catches exceptions. Its
  failure is returned without approval/resume tokens. An approval stop names its
  `recovery_tool`: inspect it with `browser_tools_call(name)`, then supply `arguments`
  to execute only the held operation through direct approval. Explicitly run only
  the remaining work in a new script—never replay the script.
  The exception is a tool the user requires approval for
  ([Tools that need your approval](your-tabs.md#tools-that-need-your-approval)): nothing
  was sent, so the script waits while the user is asked, then the request is sent again;
  that wait does not count against the deadline. A decline stops the script as above.
- Timeout/cancellation kills and reaps the worker process group. An already-sent
  bridge command can still finish; an `unknown` operation needs a fresh page read
  before another write. `ok` in the operation list means the API call succeeded,
  not proof of every intended page effect. Host access or deliberately detached
  processes are outside these browser/lifecycle guarantees.

The bridge must already be running (normal MCP startup starts it). The scripting
transport does not start/restart a bridge or retry a request. To disable the tool,
set `KITSUNE_SCRIPTING=0` and restart the MCP client; this restores the legacy
direct tool list. Keep guidance short: `help(browser.new_tab)` or `help(tab.query)` in a
script prints that method's signature/docstring on demand; the tables below give
the broader reference.

## Composition in ordinary Python

The existing API already covers navigation, reads, page actions, screenshots, waits,
inspection and browser data. The tables below are the reference; no separate tool
catalog lookup is needed. Use normal loops and conditions instead of a batch/workflow
language when writing Python.

Read an owned background tab:

```python
tab = browser.new_tab("https://example.com/")
try:
    print(tab.info()["title"])
    print(tab.text())
finally:
    tab.close()
```

Fill a draft in an explicitly chosen, authorized tab without submitting it:

```python
tab = browser.tab(draft_tab_id)
for target, value in {"#name": "Ada", "#note": "Please review this draft."}.items():
    tab.fill(target, value)
print(tab.forms())  # observe the final values, not only successful dispatches
```

Filter a bounded set of results and return only what is useful:

```python
tab = browser.tab(results_tab_id)
links = tab.query("main a[href]", fields=["text", "href"], limit=100)
selected = [link for link in links if "Python" in link.get("text", "")]
print(selected[:10])
```

If the query was truncated, it warns: these are matches **within the returned
subset**, not a complete page search. Narrow the selector or increase `limit` when
needed. Permission failures and other failed calls raise `KitsuneError` and stop an
uncaught loop; earlier writes remain applied. Do not restart the whole loop after a
timeout or disconnect—observe the affected page before deciding what is left to do.

Less-frequent features such as `tab.readability()`, `tab.network.resources()`,
`tab.local_storage`, `browser.cookies` and `browser.history` already have Python
methods. The [minimal surface mapping](minimal-tools.md#moving-less-frequent-tools-to-python)
shows which direct tools move to these APIs when scripting is enabled.

## Tabs

| `Browser` method | Returns |
| --- | --- |
| `tabs()` | `list[dict]`, not `Tab` objects: `id`, `url`, `title`, `active`, `windowId`, `index`, `owned` (this client owns it). Wrap a chosen dict with `browser.tab(t["id"])` |
| `new_tab(url="about:blank", wait=True, timeout=15000)` | A `Tab` for a new background tab. A load past `timeout` ms raises `TimedOut`; the tab stays open, with its id in `error.result["tabId"]` |
| `tab(tab_id)` | A `Tab` for an existing tab |
| `active_tab()` | The tab you are viewing. With several windows, each has one: it raises `ambiguous_target`, and you pick one from `tabs()` |

A `Tab` sends its id with every request, so it acts on that tab whether or not you are
viewing it.

| `Tab` method | Does |
| --- | --- |
| `close(force=False)` | Close this client's tab; user and other clients' tabs are refused with `user_tab_read_only` by default. `force` disarms `beforeunload` |
| `show()` | Switch your view to the tab; call it only when you want that |
| `wake()` | Reload a tab the browser discarded and wait until the page answers |
| `duplicate()` | A `Tab` for a background copy |
| `pin(pinned=True)`, `mute(muted=True)`, `zoom()`, `set_zoom(factor)` | Tab state |
| `move(window_id=None, index=-1)`, `detach()`, `resize_window(width, height)` | Place the tab, give it a window of its own, or resize its window |
| `tag(name)` | Name the tab; `browser.tags.tab(name)` finds it, `.list()` and `.remove(name)` manage names |

`browser.windows` has `list()`, `create(url=None, incognito=False)`, `close(window_id)`
and `focus(window_id)`, which takes your focus. Closing or focusing a window with user
or other clients' tabs is refused by default. `browser.pool` keeps reusable background tabs: `create(size, url)`,
`acquire()` for a `Tab`, `release(tab)`.

## Reading

| `Tab` method | Returns |
| --- | --- |
| `text()` | The visible text, as `str` |
| `snapshot(target=None, max_depth=None, text=False)` | The accessibility-style tree with element refs (`e12`), as `str` ([Snapshots](api-reference.md#browser_snapshottab_idnone-selectornone-refnone-max_depthnone-textfalse)) |
| `markdown(target=None)` | The page, or one subtree, as Markdown |
| `html(target)` | One element's `outerHTML` (`"html"` for the whole page) |
| `query(selector, fields=None, limit=50)` | Fields of matching elements up to `limit`, as a list; warns if results are truncated |
| `find(query)` | Elements matching a description such as `"login button"`, best first, each with a `ref` |
| `forms()` | Every form field with its label and value |
| `links(internal_only=False)` | The page's links |
| `info()` | URL, title, `readyState`, sizes, scroll position and recent dialogs |
| `screenshot(path=None, target=None, full_page=False, format="png")` | PNG or JPEG bytes, also written to `path`. No capture shows the tab |
| `evaluate(function, target=None)` | The JSON value a JavaScript function returns, for example `evaluate("() => document.title")` or `evaluate("el => el.value", "e3")`. Needs the developer extension build |
| `dom(target=None, max_depth=None)` | The accessibility tree of interactive elements, as nested dicts |
| `images()`, `iframes()` | The page's images and frames, as lists |
| `meta()`, `structured_data()`, `readability()` | Meta tags and `<link>`s; JSON-LD, Open Graph and Twitter cards; the main article |

Sensitive values read redacted, as for the MCP tools
([Sensitive-field redaction](api-reference.md#sensitive-field-redaction)). When the
bridge cuts a read at its size cap (page text at 20,000 characters, HTML at 100 KB,
Markdown at 80 KB), the method warns with a `UserWarning`; read a part of the page
with a `target` instead.

### Python and direct tool arguments are not aliases

| Intent | Direct MCP tool | Python API |
| --- | --- | --- |
| Find snapshot text | `browser_find(text=..., regex=...)` | `tab.find(query)` has a best-effort CSS fast path, otherwise ranks description matches such as `"login button"`; it has no `text`/`regex` keywords |
| Limit snapshot depth | `browser_snapshot(depth=...)` | `tab.snapshot(target=None, max_depth=..., text=False)`; `max_depth` and `text` are keyword-only |
| Observe geometry | `browser_snapshot(boxes=True)` | No `boxes` argument; `tab.bounds(target)` reads one element's box |

For literal/regex matching, filter a scoped read in ordinary Python rather than passing
those arguments to `find`. For example, on a page with a `main` element:

```python
import re
lines = tab.snapshot("main", text=True).splitlines()
result = [line for line in lines if re.search(r"원|USD", line)][:10]
```

This searches only the returned subtree/lines, not necessarily the whole page; heed truncation
warnings and narrow the read if needed. Do not print a full long-page snapshot to find one match.
`help(tab.find)` and `help(tab.snapshot)` return the actual signatures and these distinctions locally,
without a browser call. In `browser_run_python`, an unexpected-keyword `TypeError` for these two
methods also includes their signature/docstring. The original error remains an error: no alias,
automatic correction or retry is performed. Earlier successful writes remain done; observe the page
and continue only the unfinished work, never replay the failed script.

`evaluate` runs in the extension's content-script world, not in the page's: it sees the
page's DOM, and reaches the page's own JavaScript globals only through
`window.wrappedJSObject`, so `evaluate("() => window.wrappedJSObject.appState")`.

## Inspecting

| `Tab` method | Returns |
| --- | --- |
| `bounds(target)` | An element's box, the viewport and page sizes, and `inView` |
| `computed_style(target, properties=None)` | Computed CSS, a common set of properties by default |
| `explain_selector(selector)` | How many elements a CSS selector matches, samples, and more specific suggestions |
| `full_page_metrics()` | Document size, viewport, scroll position and device pixel ratio |
| `highlight(target)`, `clear_highlight()` | Outline an element on the page |
| `watch_console(enabled=True)`, `console_logs(since=None, level=None)` | Record the page's console, then read the messages |
| `network.start()`, `.requests(since=0)`, `.stop()`, `.clear()`, `.resources()` | The page's requests from its resource timing (no headers or bodies); `resources()` needs no `start()` |

`browser.intercept` blocks or redirects requests in every tab: `add(patterns, effect="block")`
with URL regexes, `rules()`, `log()` and `clear()`.

## Acting

Targets are a ref from `snapshot()` or `find()`, CSS, or one of the
[Playwright-style selectors](api-reference.md#target-selectors) such as
`text=Save` or `role=button[name="Save"]`. A write's selector must match one
element: otherwise it raises `ambiguous_target` and nothing runs, and `first=True`
acts on the first match ([Strict selectors](api-reference.md#strict-selectors)).

| `Tab` method | Does |
| --- | --- |
| `navigate(url)`, `back()`, `forward()`, `reload(bypass_cache=False)` | Navigate the tab |
| `click(target, button=None, modifiers=None, hold_ms=0)`, `double_click(target)`, `hover(target, duration_ms=0, steps=None)` | Pointer events (synthetic) on the target; `hold_ms` (0-10000) holds a click's press for long-press controls, and `duration_ms` (0-10000) or `steps` (1-50) keep a hover moving across the target for hover-intent controls ([POST /api/click](api-reference.md#post-apiclick), [POST /api/hover](api-reference.md#post-apihover)) |
| `fill(target, value)` | Replace a field's or editor's value |
| `type(target, text, clear=False)` | Type with key events |
| `fill_form(fields)` | Fill several fields; each key is a CSS selector, name, label or placeholder |
| `press(key, target=None, modifiers=None, text=None)` | Press `"Enter"`, `"ArrowDown"`, `"a"`, ... with modifiers such as `{"ctrl": True}` |
| `select_option(target, value, by_text=False)` | Choose an `<option>` |
| `check(target, checked=True)`, `uncheck(target)` | Set a checkbox or radio button |
| `scroll(direction="down", amount=500, target=None)` | Scroll the target or the window |
| `submit_form(target=None)` | Submit a form |
| `handle_dialogs(accept=True, prompt_text=None)` | Answer this tab's future dialogs; `None` leaves them to you again |
| `focus(target)`, `blur(target=None)` | Move focus |
| `drag(source, destination)` | HTML5 drag events, else pointer events (`via` says which) |
| `insert(target, text, html=False, clear=False)` | Insert text into an editor, input or textarea, appending unless `clear`; `html=True` (developer build) inserts HTML into an editor |
| `upload_files(paths, target=None)`, `drop(target, paths=None, data=None)` | Put local files into a file input, or drop files and data on an element |
| `trusted_click(target=None, x=0, y=0, coordinate_space="viewport", restore_pointer=True, button="left", click_count=1, modifiers=None, hold_ms=0)` | A click with the real OS pointer, for pages that ignore synthetic clicks; `button="right"`/`"middle"`, `click_count=2`, `modifiers=["Shift", ...]` and `hold_ms` (a long press) choose the click. It moves your cursor for the click, then back unless `restore_pointer=False`, and may show the tab for the click ([`browser_trusted_click`](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone-hold_ms0)) |

Actions return the bridge's result. `ok: True` only means the action was dispatched.
To check what happened, pass `expect`, for example `expect={"url_changed": True}` or
`expect={"text": "Saved"}`; an unmet outcome raises `KitsuneError` with code
`outcome_not_observed` ([Action outcomes](api-reference.md#action-outcomes)). Pointer
and typing actions also take `wait=True` to wait up to `timeout` ms for their target
first ([Actionability wait](api-reference.md#actionability-wait)). Tabs an action opened
are in its `opened_tabs`; open them with `browser.tab(id)`. A menu, listbox or dialog that
`click`, `double_click` or `hover` opened is in its `opened_popup`
([Opened popups](api-reference.md#opened-popups)).

## Waiting

`tab.wait_for(...)` waits up to `timeout` ms (default 10000) for exactly one
condition and raises `TimedOut` if it does not hold:

| Condition | Holds when |
| --- | --- |
| `target=` | the element is visible |
| `text=`, `text_gone=` | the rendered page text contains, or no longer contains, the literal text. Names from placeholders and `aria-label`s are not page text |
| `url=`, `title=` | the tab's URL or title matches a JavaScript regex; survives navigation |
| `js=` | an expression returns a non-empty result (developer build only) |
| `network_idle=True` | no new resource loaded for 500 ms |

## Batches, parallel tabs and workflows

`browser.batch(commands, tab=None)` runs [batch commands](api-reference.md#post-apibatch)
in one request, `browser.parallel(sequences)` runs one sequence per tab concurrently,
and `browser.workflow(name, variables)` runs a named workflow (`browser.workflows()`
lists them). A failed command does not raise: each one's outcome is in `results` and
`progress` ([Container progress](api-reference.md#container-progress)). A call that
did not finish `ok` returns `call` and `resume_token` for `browser.resume(...)`
([Checkpoint and resume](checkpoint-resume.md)). `browser.retry(command, max_attempts=3)`
repeats one command while it fails retryably.

For several tabs, `browser.keep_alive(tabs)` stops the browser's tab unloader from
discarding them until `stop_keep_alive()`, `browser.broadcast(tabs, command)` runs one
command in each, and `browser.sync_barrier(tabs, predicate)` waits until a JavaScript
predicate holds in all of them.

## Browser data

| Method | Returns |
| --- | --- |
| `tab.local_storage`, `tab.session_storage`: `.get(key)`, `.set(key, value)`, `.remove(key)`, `.clear()`, `.items()` | The page's storage |
| `browser.cookies.get(url=None, name=None, domain=None)` | Matching cookies |
| `browser.cookies.set(url, name, value, domain=None, path=None, secure=False)`, `.remove(url, name)`, `.clear(url=None, domain=None)` | The bridge's result |
| `browser.clipboard.read()`, `.write(text)` | The system clipboard |
| `browser.downloads.start(url, filename=None)`, `.list(query=None)`, `.cancel(download_id)` | A download id, the latest downloads |
| `browser.sessions.save(name, urls=None)`, `.load(name)`, `.list()`, `.delete(name)` | Cookies, and the localStorage of `urls`' origins, saved to disk by the bridge |
| `browser.clear_browsing_data(types, since=None)` | Remove the named kinds of data, such as `["cache"]`. `types` is required: without it the extension would also remove cookies, history and saved passwords |
| `browser.history.search(query="", since=None, limit=20)` | Visited pages, newest first; `since` is a `datetime` (naive is local time) |
| `browser.history.visits(url)` | The visits to one URL from `search` |
| `browser.bookmarks.search(query="", limit=20)` | Matching bookmarks |

With ask-before-act on, page storage, cookies, the clipboard and downloads raise
`ConfirmationRequired` for every operation, reads included
([Ask-before-act](api-reference.md#ask-before-act)).

History and bookmarks need a browser permission that only you can grant, once, with
**Allow history and bookmarks** on Kitsune's options page (about:addons → Kitsune →
Options); until then they raise `permission_required`. Results stay inside the
bridge's history scope ([History and bookmarks](api-reference.md#history-and-bookmarks)).

## Errors

A failed result raises a `KitsuneError`. Its `code`, `retryable`, `hint` and `state`
are the [action error](api-reference.md#action-errors) fields, and `result` is the
whole reply. Branch on `code`, not on the message. `retryable=False` means a write may
already have taken effect: check the page before repeating it. `needs_user` is the
exception: nothing ran, and only you can let it go on.

| Exception | Raised for |
| --- | --- |
| `ConfirmationRequired` | An action that needs your approval (`needs_user` with `state.reason` `approval`, and a token), with `token`, `expires_in`, `message` and `detail`: `confirmation_required` (ask-before-act, with `hold_reason`), or `user_tab` (closing a user tab, from a v0.7.1 extension) |
| `ConnectionFailed` (also a `ConnectionError`) | The bridge or extension was not reached or dropped the request: the [connection codes](api-reference.md#connection-diagnosis), `bridge_disconnected` and `bridge_version_mismatch` |
| `TimedOut` (also a `TimeoutError`) | `timeout`, a page load that timed out, and a wait that ran out |
| `KitsuneError` | Every other failure, such as `stale_ref`, `ambiguous_target` or `outcome_not_observed` |

Warnings that accompany a success (`warning`) do not raise.

Until #376 these approvals had their own codes, `confirmation_required` and `user_tab`.
They are now `code` `needs_user`, with the old code in `state.detail` (`error.detail`), and
an ask-before-act hold's reason in `state.holdReason` (`error.hold_reason`, before:
`state.reason`). `ConfirmationRequired` is raised for both
shapes. A `needs_user` approval without a token raises a plain `KitsuneError`: nothing can
approve it there.

## Ask-before-act

Some actions need your approval, and nothing runs until you give it: with
[ask-before-act](api-reference.md#ask-before-act) on, the bridge holds submits, payment
pages, JavaScript and cookie access (`detail` `confirmation_required`). User-tab writes
instead fail immediately with `user_tab_read_only`; approval tokens do not override tab
permissions. Actions needing approval raise
`ConfirmationRequired`; `error.message` is a short request to show you. Repeat the call
with `confirm=error.token` within
`error.expires_in` seconds, or let `connect(confirm=...)` decide: the function gets the
error, and when it returns `True` the action is sent once more with its token. In a
script, that function can ask you, so the approval comes from a person. The Python API has
no MCP elicitation, so it always gets the token, and the bridge's
[audit log](api-reference.md#audit-log) records these approvals with `approval: "token"`:

```python
def ask(held):
    return input(f"{held} -- allow? [y/N] ").strip().lower() == "y"

browser = kitsune.connect(confirm=ask)
```

## The bridge

| `Browser` method | Returns |
| --- | --- |
| `status()`, `version()`, `health()` | Connection state with its [diagnosis](api-reference.md#connection-diagnosis), versions, and the bridge's own state |
| `logs(limit=200)`, `audit(limit=100, since=None)` | The bridge's log lines; every client's commands from its [audit log](api-reference.md#audit-log) |
| `scheduler()` | Command scopes, pending commands and tab locks |
| `policy.get()`, `policy.set(allow=None, deny=None, readonly=None, ask=None, ask_urls=None, ...)` | The bridge's policy, for every client; a locked ask-before-act stays on |
| `cache(action="status", seconds=None)`, `reload_extension()` | The read cache; reload the extension |

## Any other route

`browser.request(route, body=None, timeout=15)` sends any
[bridge route](api-reference.md#http-bridge-endpoints): GET without a body, POST with
one. It raises the same errors and applies `confirm`. Tab routes take `tabId` in the
body, for example `browser.request("/api/full-page-metrics", {"tabId": tab.id})`.
