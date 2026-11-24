# Python API

The `kitsune` package drives the browser you already have open from Python, through the same
extension and bridge as the MCP server. The MCP server gives an agent a small tool surface; the
Python API reaches every capability, `storage` and `history` included. Both follow the same
[shared-browser rules](api-reference.md#shared-browser).

```bash
pip install kitsune-browser      # or: uv add kitsune-browser
```

It needs the Kitsune extension ([Installation](installation.md)). Keep scripts on the same
version as your MCP clients, since they share one bridge.

```python
import kitsune

browser = kitsune.connect()                        # starts the bridge if none runs
tab = browser.new_tab("https://news.ycombinator.com")   # a background tab
for link in tab.query(".titleline > a", fields=["text", "href"], limit=3):
    print(link["text"], link["href"])
tab.close()
```

## Connecting

`kitsune.connect()` starts the shared bridge if needed, waits for the extension and returns a
`Browser`; it raises `ConnectionFailed` if the extension is not connected.

| Argument | Default | Effect |
| --- | --- | --- |
| `user_tabs` | `read-only` | `writable` lets this client write your tabs ([Protecting your tabs](your-tabs.md#protecting-your-tabs)) |
| `confirm` | `None` | Decides on held actions; see [Ask-before-act](#ask-before-act) |
| `replace_bridge` | `False` | Replace a running bridge of another build (which drops its clients' state) |

`KITSUNE_HTTP_PORT`, `KITSUNE_TOKEN` and `KITSUNE_HOME` choose the bridge
([Configuration](configuration.md)). A bridge of another build raises `ConnectionFailed`
(`bridge_version_mismatch`).

An application that already owns a `BridgeClient` can wrap it with `Browser(existing_client)`,
keeping its identity and permissions. `Browser` is an API wrapper, not a sandbox.

## Python scripting through MCP

`browser_run_python(code, timeout=30)` is one of the [default tools](tools.md#default-tools).
It runs ordinary local Python with host file, environment, subprocess and network access:
**not a sandbox**. Approvals and tab protection apply to the supplied browser API, not to
arbitrary code. Set `KITSUNE_SCRIPTING=0` for clients you don't trust with local code.

```python
for target, value in {"#name": "Ada", "#note": "Please review."}.items():
    tab.fill(target, value)
result = tab.snapshot()
```

- `browser` is the API, routed through the MCP session. **Don't call `kitsune.connect()`.**
- `time`, `re` and `json` are preloaded; import any other module yourself.
- `tab` is the MCP-selected tab at start, else the tab you are viewing. Scripts don't change the
  MCP selection. `browser.tabs()` returns dicts with real tab ids (not MCP list indexes); wrap one
  with `browser.tab(t["id"])`.
- Each run has fresh variables and a temporary working directory; tabs persist. No top-level
  `await`.
- Return data with `print(...)` or a JSON-compatible `result`. Intermediate reads are not sent to
  the model. Use `browser_take_screenshot` for images.
- A script that prints nothing and assigns no `result` (`result = None` is an assignment) returns
  what its last operation changed, when that was a successful `click`, `type`, `fill`,
  `select_option`, `check`, `press`, `hover`, `navigate` or a `submit_form` that loaded another
  page (`navigated`, #586): `result` is
  `{"last_action": "/api/click", "snapshot": "…", "snapshot_delta": true}`, the action's own
  [post-action snapshot](api-reference.md#post-action-snapshots) with its `no_visible_change`,
  `navigated`, `snapshot_truncated` and `snapshot_hint`. Nothing is returned after a read, a wait
  or a `submit_form` that loaded nothing, for a full snapshot
  taken without the settle wait (`KITSUNE_POST_ACTION_SNAPSHOT=full`), or while a navigation is
  still loading (`navigation_pending`).
- Limits: 64,000 bytes of code; 16,000 bytes of output and result combined (bytes, not
  characters); 100 API calls; 30 s default deadline, 120 s max.
- The 101st call, or the first call after the deadline, is not sent: it raises `ScriptLimit`
  (`code` `script_operation_limit` or `script_timeout`). It is a `BaseException`, so
  `except Exception` and `except KitsuneError` let it through while `finally` and
  `except ScriptLimit` run; print what the loop collected there. The next call stops the script.
  A call still waiting for its reply at the deadline raises it too, and stays `unknown` in
  `operations`: the bridge may still run it.
  Uncaught, the script ends with that code, its printed output and its `result` variable, if it
  set one. `browser.calls_left` and `browser.seconds_left` give the budget left. When every
  operation was a read or a navigation, the hint says nothing was written.
- A failed read or wait raises its exception in the script, as outside scripts: `text`,
  `snapshot`, `find`, `query`, `forms`, `info` and `wait_for` (except `js=`, which runs JavaScript
  in the page). Catch `TimedOut` or `KitsuneError` (both are defined in the script) to go on; the
  call stays `failed` in `operations`. Uncaught, it ends the script like any failed call.
- Any other failed API call stops the script, even if caught, and so does every call that needs
  you or that policy refuses (`needs_user`, tab ownership, read-only mode), reads included. Printed
  output up to the stop is returned, `result` is not. Earlier writes stay applied: observe the
  page and run only the remaining work, never replay the script. An approval stop names a
  `recovery_tool` for `browser_tools_call`. A tool [you require approval for](your-tabs.md#tools-that-need-your-approval)
  pauses the script instead, without counting against the deadline.
- A ref dies when an action loads another document, even a reload of the same URL. Links and
  fields are followed: when the tab is your client's own and its new page lists exactly one link
  with the same name and the same destination, or (#616) is back at the URL the ref's snapshot
  was taken on and lists exactly one textbox, searchbox, combobox or spinbutton with the same name
  and the same tag, `type`, `name` and `id`, the action runs on that element and reports
  `remapped: {from, to, role, name}` in its result and its `operations` entry. Only refs from
  `tab.snapshot()` are followed this way. Any other stale ref fails as `stale_ref`: target by
  selector, use the refs `tab.navigate(url)` returns in `r['snapshot']`, or take a new snapshot.
- An agent tab dismisses a confirm or prompt no policy answers. A script whose action opened one
  returns `dialogs` (the last 5, each with its `operation` index) and, for a dismissed confirm or
  prompt, `dialog_hint`, whether or not it printed the action's reply; that operation's entry has
  `dialog: "dismissed"` (or `"shown"`). Call `tab.handle_dialogs(accept=True)` and repeat the
  action, in the same script if you like.
- The worker is killed 2 s after the deadline; a command already sent may still finish, so read
  the page before writing again.
- `help(tab.query)` prints a method's signature. A `Tab` or `Browser` call with unknown, missing or
  extra arguments fails with a `TypeError` and that signature in the output.

Keep output bounded on long pages: read a known region or a bounded index first, then a scoped
snapshot of the section you found.

```python
links = tab.query("nav a[href]", fields=["text", "href"], limit=200)
matches = [l for l in links if "delivery" in l.get("text", "").casefold()]
result = {"matches": matches[:5], "more": len(matches) > 5}
```

Output and result over the limit are cut, not failed: the script runs to its end and the reply has
`ok: true`, `truncated: true` and `dropped_bytes` (the UTF-8 bytes left out). Printed output keeps
its first 16,000 bytes; `result` gets what the output left, a string cut to its start and any
other value to the start of its JSON text (a string). The cut content is incomplete: no match in
it is not absence from the page, so make a new, smaller read-only observation for the rest.
`operations` lists what the script did; do not replay it.

Each `operations` entry has `route`, `status` (`ok`, `failed`, or `unknown` for a call cut off by
the deadline), `tab_id`, `remapped` when a ref was followed, and `method` only when it is `GET`.
Over 10 operations the list is summarized:
`{"count": 100, "by_route": {"/api/navigate": 50, "/api/page-text": 50}, "tab_id": 7, "notable": [...], "last": [...]}`.
`notable` keeps every entry that failed, is `unknown`, was remapped or used another tab (up to
10, then `notable_more`), and `last` the last 3 others, each with its `index`.

## Tabs

| `Browser` method | Returns |
| --- | --- |
| `tabs()` | `list[dict]`: `id`, `url`, `title`, `active`, `windowId`, `index`, `owned` |
| `new_tab(url="about:blank", wait=True, timeout=15000)` | A `Tab` for a new background tab. A load still pending after `timeout` raises `TimedOut`, with the tab id in `error.result["tabId"]`, unless the page's DOM is parsed by then |
| `tab(tab_id)` | A `Tab` for an existing tab |
| `active_tab()` | The tab you are viewing (`ambiguous_target` with several windows) |

| `Tab` method | Does |
| --- | --- |
| `close(force=False)` | Close this client's tab; `force` disarms `beforeunload` |
| `show()` | Switch your view to the tab |
| `wake()` | Reload a discarded tab |
| `duplicate()` | A background copy |
| `pin()`, `mute()`, `zoom()`, `set_zoom(factor)` | Tab state |
| `move(window_id=None, index=-1)`, `detach()`, `resize_window(width, height)` | Placement |
| `tag(name)` | Name the tab; `browser.tags.tab(name)`, `.list()`, `.remove(name)` |

`browser.windows`: `list()`, `create(url=None, incognito=False)`, `close(id)`, `focus(id)`.
`browser.pool`: `create(size, url)`, `acquire()`, `release(tab)`.

## Reading

| `Tab` method | Returns |
| --- | --- |
| `text()` | Visible text |
| `snapshot(target=None, *, max_depth=None, text=False)` | Accessibility-style tree with refs ([Snapshots](api-reference.md#browser_snapshottab_idnone-selectornone-refnone-max_depthnone-textfalse)) |
| `markdown(target=None)` | The page or a subtree as Markdown; an input or textarea target reads as its exact value, line breaks included |
| `html(target)` | One element's `outerHTML` (`"html"` for the page) |
| `query(selector, fields=None, limit=50)` | Fields of matching elements (max 500 rows, 400-char snippets); warns when truncated, and when nothing matches a tag that elements carry as their role (`article` for `role="article"`). `href` and `src` are absolute URLs, even when the attribute is relative |
| `find(query)` | Elements matching a description like `"login button"`, best first |
| `forms()` | Form fields with labels and values |
| `links(internal_only=False)` | Links |
| `info()` | URL, title, `readyState`, sizes, scroll, recent dialogs |
| `screenshot(path=None, target=None, full_page=False, format="png")` | Image bytes, optionally written to `path` |
| `evaluate(function, target=None)` | A JS function's JSON result (developer build). Page globals via `window.wrappedJSObject` |
| `dom(target=None, max_depth=None)` | Interactive elements as nested dicts |
| `images()`, `iframes()`, `meta()`, `structured_data()`, `readability()` | Images, frames, meta tags, JSON-LD/Open Graph, main article |

Sensitive values are redacted ([Sensitive-field redaction](api-reference.md#sensitive-field-redaction)).
Reads cut at the bridge's caps warn with a `UserWarning`; pass a `target` to read less (`text()`
has none: use `snapshot(target, text=True)` or `query`). `text()` reads up to 500,000 characters,
not the direct tool's 20,000: a script's reads are not model-visible, only what it prints (#630).

### Selector contract by method

| Method | CSS | Ref (`e12`) | [Playwright-style](api-reference.md#target-selectors) | Matching |
| --- | --- | --- | --- | --- |
| Actions (`click`, `fill`, …) | Yes | Yes | Yes | Strict: ambiguous matches are refused |
| `snapshot(target)` | Yes | Yes | Yes | First match |
| `query(selector)` | Yes | No | No | Current document only |
| `wait_for(target=)` | Yes | Yes | No | First visible match |

`tab.find(query)` is not `browser_find`: it has no `text`/`regex` arguments. For literal or regex
matching, filter a scoped read in Python:

```python
import re
lines = tab.snapshot("main", text=True).splitlines()
result = [line for line in lines if re.search(r"원|USD", line)][:10]
```

## Inspecting

| `Tab` method | Returns |
| --- | --- |
| `bounds(target)` | Element box, viewport and page sizes, `inView` |
| `computed_style(target, properties=None)` | Computed CSS |
| `explain_selector(selector)` | Match count, samples, suggestions |
| `full_page_metrics()` | Document size, viewport, scroll, pixel ratio |
| `highlight(target)`, `clear_highlight()` | Outline an element |
| `watch_console(enabled=True)`, `console_logs(since=None, level=None)` | Console messages |
| `network.start()`, `.requests(since=0)`, `.stop()`, `.clear()`, `.resources()` | Requests from resource timing (no headers or bodies) |

`browser.intercept`: `add(patterns, effect="block")`, `rules()`, `log()`, `clear()`.

## Acting

Targets are a ref, CSS or a [Playwright-style selector](api-reference.md#target-selectors).
Writes need one match, else `ambiguous_target`; `first=True` takes the first. `click` and
`hover` take links to the same URL, or the one better `text=` match, as one target and
report `matches` ([Strict selectors](api-reference.md#strict-selectors)).

| `Tab` method | Does |
| --- | --- |
| `navigate(url)`, `back()`, `forward()`, `reload(bypass_cache=False)` | Navigate. `url` may be relative to the tab's current URL (`"/deals/48"`); one that does not parse fails with `invalid_url`. A page that still looks like it is rendering (no content, a `Loading...` line, `aria-busy`) warns with the `wait_for` to use; `new_tab` warns the same way |
| `click(target, button=None, modifiers=None, hold_ms=0)`, `double_click(target)` | Synthetic click; `hold_ms` for long presses |
| `hover(target, duration_ms=0, steps=None)` | Synthetic hover; `duration_ms`/`steps` for hover-intent controls |
| `fill(target, value)` | Replace a value |
| `type(target, text, clear=False)` | Append to input/textarea; insert at the caret in contenteditable |
| `fill_form(fields)` | Fill several fields keyed by selector, name, label or placeholder |
| `press(key, target=None, modifiers=None, text=None)` | Key press, e.g. `"Enter"` with `{"ctrl": True}` |
| `select_option(target, value, by_text=False)` | Choose an `<option>` |
| `check(target, checked=True)`, `uncheck(target)` | Checkbox or radio; an element with role `checkbox`, `switch` or `radio` is clicked once when `aria-checked` differs ([details](api-reference.md#post-apicheck)) |
| `scroll(direction="down", amount=500, target=None)` | Scroll |
| `submit_form(target=None)` | Submit a form |
| `handle_dialogs(accept=True, prompt_text=None)` | Answer future dialogs |
| `focus(target)`, `blur(target=None)` | Focus |
| `drag(source, destination)` | HTML5 drag, else pointer events |
| `insert(target, text, html=False, clear=False)` | Insert at an editor's caret |
| `upload_files(paths, target=None)`, `drop(target, paths=None, data=None)` | Files into an input; drop files and data |
| `trusted_click(target=None, …, restore_pointer=True, button="left", click_count=1, modifiers=None, hold_ms=0)` | A real OS click; moves your cursor ([details](api-reference.md#browser_trusted_clickselector-x0-y0-coordinate_spaceviewport-focusfalse-tab_idnone-firstfalse-restore_pointertrue-buttonleft-click_count1-modifiersnone-hold_ms0)) |

`ok: True` only means dispatched. Pass `expect`, e.g. `expect={"text": "Saved"}`, to raise
`outcome_not_observed` when the effect doesn't appear ([Action outcomes](api-reference.md#action-outcomes)),
and `wait=True` to wait for the target first ([Actionability wait](api-reference.md#actionability-wait)).
Results list `opened_tabs` and, for clicks and hovers, `opened_popup`
([Opened popups](api-reference.md#opened-popups)). `click`, `type`, `fill`, `select_option`,
`check`, `press` and `hover` also return what they changed, with refs, under `snapshot`
([Post-action snapshots](api-reference.md#post-action-snapshots)), so a full `snapshot()` is
needed only when that is not enough.

### Editing and focus

`fill` replaces a value; `type` appends (or inserts at the caret in contenteditable). For a
caret-sensitive edit in a native field, use `press` with a character.
`press(target=...)` dispatches to that element without focusing it; without a target it uses the
current focus. Check the result's `effect` and `prevented`, and read the page, rather than trusting
`ok`.

## Waiting

`tab.wait_for(...)` waits up to `timeout` ms (default 10000) for one condition, else `TimedOut`
(`code` `timeout`, `retryable=True`):

| Condition | Holds when |
| --- | --- |
| `target=` | The first CSS or ref match is visible |
| `text=`, `text_gone=` | Rendered text contains, or no longer contains, the literal text |
| `url=`, `title=` | URL or title matches a JS regex |
| `js=` | An expression returns a non-empty result (developer build) |
| `network_idle=True` | No new resource for 500 ms |

## Batches, parallel tabs and workflows

`browser.batch(commands, tab=None)` runs [batch commands](api-reference.md#post-apibatch) in one
request, `browser.parallel(sequences)` runs one sequence per tab, and
`browser.workflow(name, variables)` runs a named workflow (`browser.workflows()` lists them).
Failures don't raise: see `results` and `progress`. An unfinished call returns `call` and
`resume_token` for `browser.resume(...)` ([Checkpoint and resume](checkpoint-resume.md)).
`browser.retry(command, max_attempts=3)` retries retryable failures.

`browser.keep_alive(tabs)` / `stop_keep_alive()` prevent tab unloading,
`browser.broadcast(tabs, command)` runs a command in each tab, and
`browser.sync_barrier(tabs, predicate)` waits for a JS predicate in all of them.

## Browser data

| Method | Returns |
| --- | --- |
| `tab.local_storage`, `tab.session_storage`: `.get`, `.set`, `.remove`, `.clear`, `.items` | Page storage |
| `browser.cookies.get(url=None, name=None, domain=None)`, `.set(...)`, `.remove(url, name)`, `.clear(...)` | Cookies |
| `browser.clipboard.read()`, `.write(text)` | System clipboard |
| `browser.downloads.start(url, filename=None)`, `.list(query=None)`, `.cancel(id)` | Downloads |
| `browser.sessions.save(name, urls=None)`, `.load(name)`, `.list()`, `.delete(name)` | Cookies and localStorage saved to disk |
| `browser.clear_browsing_data(types, since=None)` | Remove data kinds, e.g. `["cache"]` (`types` is required) |
| `browser.history.search(query="", since=None, limit=20)`, `.visits(url)` | History, newest first |
| `browser.bookmarks.search(query="", limit=20)` | Bookmarks |

History and bookmarks need **Allow history and bookmarks** in Kitsune's options; until then they
raise `permission_required` ([History and bookmarks](api-reference.md#history-and-bookmarks)).

## Errors

Failures raise `KitsuneError` with `code`, `retryable`, `hint`, `state` and the full `result`
([Action errors](api-reference.md#action-errors)). Branch on `code`. `retryable=False` means a
write may have taken effect; check the page before repeating.

| Exception | Raised for |
| --- | --- |
| `ConfirmationRequired` | An action held for approval, with `token`, `expires_in`, `message`, `detail` |
| `ConnectionFailed` (`ConnectionError`) | [Connection codes](api-reference.md#connection-diagnosis), `bridge_disconnected`, `bridge_version_mismatch` |
| `TimedOut` (`TimeoutError`) | Timeouts and expired waits |
| `KitsuneError` | Everything else (`stale_ref`, `ambiguous_target`, `outcome_not_observed`, …) |

## Ask-before-act

With [ask-before-act](api-reference.md#ask-before-act) on, held actions raise
`ConfirmationRequired`. Repeat the call with `confirm=error.token` within `error.expires_in`
seconds, or pass a `confirm` function to `connect()` that decides and returns `True` to resend:

```python
def ask(held):
    return input(f"{held} -- allow? [y/N] ").strip().lower() == "y"

browser = kitsune.connect(confirm=ask)
```

User-tab writes fail with `user_tab_read_only` instead; tokens don't override tab permissions.

## The bridge

| `Browser` method | Returns |
| --- | --- |
| `status()`, `version()`, `health()` | Connection state and [diagnosis](api-reference.md#connection-diagnosis), versions |
| `logs(limit=200)`, `audit(limit=100, since=None)` | Bridge log; [audit log](api-reference.md#audit-log) |
| `scheduler()` | Pending commands and tab locks |
| `policy.get()`, `policy.set(allow=None, deny=None, readonly=None, ask=None, ...)` | Bridge policy |
| `cache(action="status", seconds=None)`, `reload_extension()` | Read cache; reload the extension |

`browser.request(route, body=None, timeout=15)` calls any
[bridge route](api-reference.md#http-bridge-endpoints), e.g.
`browser.request("/api/full-page-metrics", {"tabId": tab.id})`.
