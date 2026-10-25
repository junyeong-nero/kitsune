# Python API

The `kitsune` package drives the browser you already have open from Python, through
the same extension and bridge as the MCP server. The two have different jobs:

- **The MCP server** gives an agent a small, curated tool surface. `KITSUNE_CAPS`,
  `KITSUNE_PUBLISH` and `KITSUNE_MODE` limit what it sees and what the bridge
  accepts from it.
- **The Python API** reaches every capability of the extension and bridge, for
  scripts and for your own agent frameworks. Its requests carry no capability
  header, so every group is available, `storage` and `history` included.

Both are clients of one shared bridge, and the bridge's
[shared-browser rules](api-reference.md#shared-browser) hold for both: tabs open in
the background, writes wait while you are using a tab, your own tabs close only with
your approval, and sensitive fields are redacted. Ask-before-act, when it is on,
holds the same actions.

```bash
pip install kitsune-browser-mcp      # or: uv add kitsune-browser-mcp
```

The package is the one the MCP server ships in; it needs the Kitsune extension
installed in Firefox, Zen or LibreWolf ([Installation](installation.md)).

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
| `mode` | `"act"` | `"read"` or `"draft"` restricts requests as [`KITSUNE_MODE`](api-reference.md#task-modes) does for MCP |
| `protect_user_tabs` | `False` | `True` refuses navigation in tabs no client opened, as [`KITSUNE_PROTECT_USER_TABS`](api-reference.md#protected-user-tabs) |
| `confirm` | `None` | A function that decides on actions ask-before-act holds; see [Ask-before-act](#ask-before-act) |
| `replace_bridge` | `False` | `True` replaces a running bridge of another build |

`KITSUNE_HTTP_PORT`, `KITSUNE_TOKEN` and `KITSUNE_HOME` choose the bridge, as for the
MCP server ([Configuration](guide.md#configuration)).

A script and your MCP clients share the bridge, so they should run the same Kitsune
version from the same source. A bridge of another build raises `ConnectionFailed`
with code `bridge_version_mismatch` and keeps running. The MCP server would replace
it, but replacing a bridge drops its clients' keep-alives, intercepts and resume
checkpoints, so the Python API does that only with `replace_bridge=True` (or after
`kitsune-bridge stop`).

## Tabs

| `Browser` method | Returns |
| --- | --- |
| `tabs()` | Every open tab: `id`, `url`, `title`, `active`, `windowId`, `index`, `owned` (a client opened it) |
| `new_tab(url="about:blank", wait=True, timeout=15000)` | A `Tab` for a new background tab. A load past `timeout` ms raises `TimedOut`; the tab stays open, with its id in `error.result["tabId"]` |
| `tab(tab_id)` | A `Tab` for an existing tab |
| `active_tab()` | The tab you are viewing. With several windows, each has one: it raises `ambiguous_target`, and you pick one from `tabs()` |

A `Tab` sends its id with every request, so it acts on that tab whether or not you are
viewing it. `tab.show()` switches your view to it; call it only when you want that.
`tab.close(force=False)` closes a tab a client opened; closing one of your own tabs
needs your approval ([Ask-before-act](#ask-before-act)). `force` disarms
`beforeunload`.
`tab.wake()` reloads a tab the browser discarded.

## Reading

| `Tab` method | Returns |
| --- | --- |
| `text()` | The visible text, as `str` |
| `snapshot(target=None, max_depth=None, text=False)` | The accessibility-style tree with element refs (`e12`), as `str` ([Snapshots](api-reference.md#browser_snapshottab_idnone-selectornone-refnone-max_depthnone-textfalse)) |
| `markdown(target=None)` | The page, or one subtree, as Markdown |
| `html(target)` | One element's `outerHTML` (`"html"` for the whole page) |
| `query(selector, fields=None, limit=50)` | Fields of every element matching a CSS selector, as a list |
| `find(query)` | Elements matching a description such as `"login button"`, best first, each with a `ref` |
| `forms()` | Every form field with its label and value |
| `links(internal_only=False)` | The page's links |
| `info()` | URL, title, `readyState`, sizes, scroll position and recent dialogs |
| `screenshot(path=None, target=None, full_page=False, format="png")` | PNG or JPEG bytes, also written to `path`. No capture shows the tab |
| `evaluate(function, target=None)` | The JSON value a JavaScript function returns, for example `evaluate("() => document.title")` or `evaluate("el => el.value", "e3")`. Needs the developer extension build |

Sensitive values read redacted, as for the MCP tools
([Sensitive-field redaction](api-reference.md#sensitive-field-redaction)). When the
bridge cuts a read at its size cap (page text at 20,000 characters, HTML at 100 KB,
Markdown at 80 KB), the method warns with a `UserWarning`; read a part of the page
with a `target` instead.

## Acting

Targets are a ref from `snapshot()` or `find()`, CSS, or one of the
[Playwright-style selectors](api-reference.md#target-selectors) such as
`text=Save` or `role=button[name="Save"]`. A write's selector must match one
element: otherwise it raises `ambiguous_target` and nothing runs, and `first=True`
acts on the first match ([Strict selectors](api-reference.md#strict-selectors)).

| `Tab` method | Does |
| --- | --- |
| `navigate(url)`, `back()`, `forward()`, `reload(bypass_cache=False)` | Navigate the tab |
| `click(target, button=None, modifiers=None)`, `double_click(target)`, `hover(target)` | Pointer events (synthetic) on the target |
| `fill(target, value)` | Replace a field's or editor's value |
| `type(target, text, clear=False)` | Type with key events |
| `fill_form(fields)` | Fill several fields; each key is a CSS selector, name, label or placeholder |
| `press(key, target=None, modifiers=None, text=None)` | Press `"Enter"`, `"ArrowDown"`, `"a"`, ... with modifiers such as `{"ctrl": True}` |
| `select_option(target, value, by_text=False)` | Choose an `<option>` |
| `check(target, checked=True)`, `uncheck(target)` | Set a checkbox or radio button |
| `scroll(direction="down", amount=500, target=None)` | Scroll the target or the window |
| `submit_form(target=None)` | Submit a form |
| `handle_dialogs(accept=True, prompt_text=None)` | Answer this tab's future dialogs; `None` leaves them to you again |

Actions return the bridge's result. `ok: True` only means the action was dispatched.
To check what happened, pass `expect`, for example `expect={"url_changed": True}` or
`expect={"text": "Saved"}`; an unmet outcome raises `KitsuneError` with code
`outcome_not_observed` ([Action outcomes](api-reference.md#action-outcomes)). Pointer
and typing actions also take `wait=True` to wait up to `timeout` ms for their target
first ([Actionability wait](api-reference.md#actionability-wait)). Tabs an action opened
are in its `opened_tabs`; open them with `browser.tab(id)`.

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
([Checkpoint and resume](checkpoint-resume.md)).

## Cookies, history and bookmarks

| Method | Returns |
| --- | --- |
| `browser.cookies.get(url=None, name=None, domain=None)` | Matching cookies |
| `browser.cookies.set(url, name, value, domain=None, path=None, secure=False)`, `.remove(url, name)`, `.clear(url=None, domain=None)` | The bridge's result |
| `browser.history.search(query="", since=None, limit=20)` | Visited pages, newest first; `since` is a `datetime` (naive is local time) |
| `browser.history.visits(url)` | The visits to one URL from `search` |
| `browser.bookmarks.search(query="", limit=20)` | Matching bookmarks |

History and bookmarks need a browser permission that only you can grant, once, with
**Allow history and bookmarks** on Kitsune's options page (about:addons → Kitsune →
Options); until then they raise `permission_required`. Results stay inside the
bridge's history scope ([History and bookmarks](api-reference.md#history-and-bookmarks)).

## Errors

A failed result raises a `KitsuneError`. Its `code`, `retryable`, `hint` and `state`
are the [action error](api-reference.md#action-errors) fields, and `result` is the
whole reply. Branch on `code`, not on the message. `retryable=False` means a write may
already have taken effect: check the page before repeating it.

| Exception | Raised for |
| --- | --- |
| `ConfirmationRequired` | An action that needs your approval, with `token` and `expires_in`: `confirmation_required` (ask-before-act) or `user_tab` |
| `ConnectionFailed` (also a `ConnectionError`) | The bridge or extension was not reached or dropped the request: the [connection codes](api-reference.md#connection-diagnosis), `bridge_disconnected` and `bridge_version_mismatch` |
| `TimedOut` (also a `TimeoutError`) | `timeout`, a page load that timed out, and a wait that ran out |
| `KitsuneError` | Every other failure, such as `stale_ref`, `ambiguous_target` or `outcome_not_observed` |

Warnings that accompany a success (`warning`) do not raise.

## Ask-before-act

Some actions need your approval, and nothing runs until you give it: with
[ask-before-act](api-reference.md#ask-before-act) on, the bridge holds submits, payment
pages, JavaScript and cookie access (`confirmation_required`), and it never closes one of
your own tabs, or with `protect_user_tabs` navigates it, without approval (`user_tab`,
[Agent-owned tabs](api-reference.md#agent-owned-tabs)). These raise
`ConfirmationRequired`. Repeat the call with `confirm=error.token` within
`error.expires_in` seconds, or let `connect(confirm=...)` decide: the function gets the
error, and when it returns `True` the action is sent once more with its token. In a
script, that function can ask you, so the approval comes from a person:

```python
def ask(held):
    return input(f"{held} -- allow? [y/N] ").strip().lower() == "y"

browser = kitsune.connect(confirm=ask)
```

## Any other route

`browser.request(route, body=None, timeout=15)` sends any
[bridge route](api-reference.md#http-bridge-endpoints): GET without a body, POST with
one. It raises the same errors and applies `confirm`. Tab routes take `tabId` in the
body, for example `browser.request("/api/meta", {"tabId": tab.id})`.
