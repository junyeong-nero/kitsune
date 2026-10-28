"""The core tools: Playwright MCP's core tool set on Kitsune.

The core group publishes exactly the tools of @playwright/mcp with its default capabilities.
Names, descriptions, input schemas and annotations come verbatim from playwright_tools.json,
captured from its tools/list (refresh it with scripts/update_playwright_tools.mjs); each tool
here maps its arguments onto the Kitsune bridge and returns Kitsune's JSON results.
Kitsune drives the user's own browser through an extension, not a Playwright page, so
browser_emulate_media and browser_run_code_unsafe answer not_supported.

Tools act on the current tab, which browser_tabs new or select sets; select does not switch the
user's view. Kitsune's own tools that are called without tab_id act on it too. Until there is
one, reads act on the user's active tab and the MCP server refuses writes and navigations
(no_current_tab, kitsune.tools._on_current_tab). browser_navigate and browser_navigate_back
also take a tab_id that playwright_tools.json does not publish, like confirm_token, and
browser_press_key a repeat count (1-100) that the extension presses within one request.

browser_fill_form type slider and browser_type fill set an ARIA slider or spinbutton (no native
input) by pressing arrow keys in the extension until aria-valuenow is the value (#311).

Relative file names resolve against KITSUNE_OUTPUT_DIR, else the MCP client's first file root,
else the MCP server's working directory.
"""

import json
import os
import re
import sys
from contextvars import ContextVar
from pathlib import Path

SCHEMAS = {tool["name"]: tool for tool in
           json.loads(Path(__file__).with_name("playwright_tools.json").read_text(encoding="utf-8"))["tools"]}

k = None  # kitsune.tools, set by register()
_TOOLS = []
_current_tab: int | None = None
# The target last clicked per tab: browser_file_upload fills the file input it opened.
_clicked: dict[int | None, str] = {}
# The list browser_network_requests last returned per tab, which browser_network_request indexes.
_requests: dict[int | None, list[dict]] = {}
# The MCP client's first file root for this tool call; kitsune.server sets it around core calls
# with a filename.
client_root: ContextVar[Path | None] = ContextVar("client_root", default=None)

_REF = re.compile(r"e\d+")
_FIELD_TYPES = {"textbox", "slider", "checkbox", "radio", "combobox"}
# Resource timing initiators of static resources (browser_network_requests static=false).
_STATIC = {"img", "image", "css", "link", "script", "font", "video", "audio", "track", "embed",
           "object", "icon", "input"}
# Kitsune console levels each Playwright level includes (each includes the more severe ones).
_LEVELS = {"error": {"error"}, "warning": {"error", "warn"}, "info": {"error", "warn", "info", "log"},
           "debug": {"error", "warn", "info", "log", "debug"}}
_MODIFIERS = {"Alt": "alt", "Control": "ctrl", "Meta": "meta", "Shift": "shift",
              "ControlOrMeta": "meta" if sys.platform == "darwin" else "ctrl"}


def _core(function):
    _TOOLS.append(function)
    return function


def register(tools):
    """Add the core tools to kitsune.tools, which they are built on (called once, at its import)."""
    global k
    k = tools
    missing = set(SCHEMAS) ^ {function.__name__ for function in _TOOLS}
    if missing:
        raise RuntimeError(f"core tools and playwright_tools.json differ: {', '.join(sorted(missing))}")
    for function in _TOOLS:
        tools.tool("core")(function)


def _not_supported(name: str, hint: str) -> dict:
    return {"error": f"{name} is not supported by Kitsune", "code": "not_supported", "retryable": False,
            "hint": hint}


def _target(target: str | None) -> dict:
    """A snapshot ref (e12) as ref=, anything else as a CSS selector."""
    if target is None:
        return {}
    return {"ref": target} if _REF.fullmatch(target) else {"selector": target}


def _selector(target: str) -> tuple[str, str | None]:
    """(selector, ref) for request bodies; the extension reads a ref given as selector too."""
    return ("", target) if _REF.fullmatch(target) else (target, None)


def _path(filename: str) -> Path:
    """A file name resolved against the workspace root; absolute names are kept.

    The workspace root is KITSUNE_OUTPUT_DIR, else the MCP client's first file root, else
    the MCP server's working directory.
    """
    output_dir = os.environ.get("KITSUNE_OUTPUT_DIR")
    workspace = Path(output_dir).expanduser() if output_dir else client_root.get() or Path.cwd()
    return Path.cwd() / workspace / Path(filename).expanduser()


def _saved(filename: str, content: str) -> dict:
    path = _path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "saved_to": str(path)}


def _tabs() -> dict:
    """The open tabs; forgets a current tab that is gone."""
    global _current_tab
    result = k._get("/api/tabs")
    tabs = result.get("tabs")
    if isinstance(tabs, list) and _current_tab is not None and all(tab.get("id") != _current_tab for tab in tabs):
        _current_tab = None
    return result


def _tab_list() -> dict:
    """The open tabs as browser_tabs list returns them, by index."""
    result = _tabs()
    if k._failed(result):
        return result
    return {"tabs": [{"index": i, "title": tab.get("title"), "url": tab.get("url"),
                      **({"current": True} if tab.get("id") == _current_tab else {}),
                      **({"active": True} if tab.get("active") else {}),
                      **({"owned": True} if tab.get("owned") else {})}
                     for i, tab in enumerate(result.get("tabs") or [])]}


def _with_tab_list(result: dict) -> dict:
    """An action's result with the tabs after it, as list returns them.

    A failed listing leaves the result as it is: the action itself already happened.
    """
    listed = _tab_list()
    return result if k._failed(listed) else {**result, **listed}


def _tab_at(index) -> tuple[int | None, dict | None]:
    """(tab id, None) for a browser_tabs index, else (None, error)."""
    result = _tabs()
    if k._failed(result):
        return None, result
    tabs = result.get("tabs") or []
    if isinstance(index, bool) or index != int(index) or not 0 <= int(index) < len(tabs):
        return None, {"error": f"Tab {index} not found", "code": "not_found", "retryable": False,
                      "hint": "Use an index from browser_tabs action=list."}
    return tabs[int(index)]["id"], None


@_core
def browser_close(confirm_token: str | None = None) -> dict:
    return browser_tabs("close", confirm_token=confirm_token)


@_core
def browser_resize(width: float, height: float) -> dict:
    return k._post("/api/resize-window", k._request_body({"width": width, "height": height}, _current_tab))


@_core
def browser_console_messages(level: str = "info", all: bool | None = None, filename: str | None = None) -> dict:
    # Kitsune buffers per document, so all (messages before the last navigation) has nothing more.
    if level not in _LEVELS:
        return {"error": "level must be error, warning, info or debug"}
    k._post("/api/watch-console", k._request_body({"enabled": True}, _current_tab))
    result = k._post("/api/console-logs", k._request_body({}, _current_tab))
    if k._failed(result):
        return result
    logs = [log for log in result.get("logs") or [] if log.get("level") in _LEVELS[level]]
    if filename:
        return {**_saved(filename, "".join(f"[{log.get('level')}] {log.get('message')}\n" for log in logs)),
                "count": len(logs)}
    return {"count": len(logs), "logs": logs}


@_core
def browser_handle_dialog(accept: bool, promptText: str | None = None, confirm_token: str | None = None) -> dict:
    return k.browser_handle_dialog(accept=accept, prompt_text=promptText, tab_id=_current_tab,
                                   confirm_token=confirm_token)


@_core
def browser_emulate_media(colorScheme: str | None = None, reducedMotion: str | None = None,
                          forcedColors: str | None = None, contrast: str | None = None,
                          media: str | None = None) -> dict:
    return _not_supported("browser_emulate_media",
                          "A browser extension cannot override CSS media features for one page.")


@_core
def browser_evaluate(function: str, element: str | None = None, target: str | None = None,
                     filename: str | None = None, confirm_token: str | None = None) -> dict:
    # The extension binds the target element as __kitsuneElement and awaits a promise result.
    argument = "__kitsuneElement" if target is not None else ""
    code = f"Promise.resolve(({function})({argument})).then(value => JSON.stringify(value) ?? 'undefined')"
    body = {"code": code}
    if target is not None:
        body["selector"] = target
    result = k._post("/api/js", k._request_body(body, _current_tab, element=element, confirm_token=confirm_token))
    if k._failed(result) or not isinstance(result.get("result"), str):
        return result
    if filename:
        return _saved(filename, result["result"])
    try:
        value = json.loads(result["result"])
    except ValueError:
        value = result["result"]  # undefined, or a result cut at the size limit
    return {**result, "result": value}


@_core
def browser_file_upload(paths: list[str] | None = None, confirm_token: str | None = None) -> dict:
    # No file chooser opens for a synthetic click, so the files go to the input last clicked
    # (or the one inside it), else the page's only file input.
    target = _clicked.pop(_current_tab, None)
    if not paths:
        return {"ok": True, "cancelled": True}
    return k.browser_upload_file(path=paths, selector=target, mode="input", tab_id=_current_tab,
                                 confirm_token=confirm_token)


@_core
def browser_drop(target: str, element: str | None = None, paths: list[str] | None = None,
                 data: dict[str, str] | None = None, confirm_token: str | None = None) -> dict:
    if not paths and not data:
        return {"error": 'At least one of "paths" or "data" must be provided'}
    body = {"paths": paths or [], "mode": "drop", "selector": target}
    if data:
        body["data"] = data
    return k._post("/api/upload-file", k._request_body(body, _current_tab, confirm_token=confirm_token), timeout=320)


def _search(text: str | None, regex: str | None):
    """A line matcher for browser_find, or an error dict."""
    if (text is None) == (regex is None):
        return {"error": "Provide either text or regex, not both."}
    if text is not None:
        needle = text.lower()
        return lambda line: needle in line.lower()
    match = re.fullmatch(r"/(.*)/([a-z]*)", regex, re.S)
    pattern, flags = (match[1], match[2]) if match else (regex, "")
    try:
        compiled = re.compile(pattern, (re.I if "i" in flags else 0) | (re.M if "m" in flags else 0)
                              | (re.S if "s" in flags else 0))
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}
    return lambda line: compiled.search(line) is not None


@_core
def browser_find(text: str | None = None, regex: str | None = None, filename: str | None = None) -> dict:
    matches = _search(text, regex)
    if isinstance(matches, dict):
        return matches
    # Every item of a repeated sibling run, including those a snapshot collapses.
    result = k._post("/api/snapshot", k._request_body({"collapse": False}, _current_tab))
    if k._failed(result):
        return result
    lines = (result.get("snapshot") or "").split("\n")
    indent = [len(line) - len(line.lstrip(" ")) for line in lines]
    found = []
    for i, line in enumerate(lines):
        if not line or not matches(line):
            continue
        path, depth = [], indent[i]
        for j in range(i - 1, -1, -1):
            if indent[j] < depth:
                path.insert(0, lines[j].strip())
                depth = indent[j]
        found.append({"path": path, "lines": lines[max(0, i - 2):i + 3], "match": line.strip()})
    if filename:
        text_out = "\n\n".join("\n".join([" > ".join(m["path"]) or "(root)", *m["lines"]]) for m in found)
        return {**_saved(filename, text_out), "count": len(found)}
    return {"count": len(found), "matches": found}


@_core
def browser_fill_form(fields: list[dict], confirm_token: str | None = None) -> dict:
    # Fields that cannot be filled fail before any is, so the form is not left half filled.
    for field in fields:
        if not isinstance(field.get("target"), str):
            return {"error": "Each field needs a target", "field": field.get("name"), "filled": []}
        if field.get("type") not in _FIELD_TYPES:
            return {"error": f"Unknown field type: {field.get('type')}", "field": field.get("name"), "filled": []}
    filled, last = [], {}
    for field in fields:
        target, kind, value = field["target"], field["type"], field.get("value")
        element = field.get("element") or field.get("name")
        if kind in ("textbox", "slider"):
            last = k.browser_type(text=str(value), mode="fill", tab_id=_current_tab, element=element,
                                  confirm_token=confirm_token, **_target(target))
        elif kind in ("checkbox", "radio"):
            last = k.browser_check(checked=str(value).lower() == "true", tab_id=_current_tab, element=element,
                                   confirm_token=confirm_token, **_target(target))
        else:
            last = _select(target, [str(value)], element, confirm_token)
        if k._failed(last):
            return {**last, "field": field.get("name"), "filled": filled}
        filled.append(field.get("name"))
    return {**last, "ok": True, "filled": filled}


def _key(key: str) -> tuple[str, dict | None]:
    """A Playwright key ("Control+Shift+A") as Kitsune's key and modifiers."""
    *mods, name = key.split("+") if len(key) > 1 and not key.endswith("+") else [key]
    if not name or any(mod not in _MODIFIERS for mod in mods):
        return key, None
    return name, {_MODIFIERS[mod]: True for mod in mods} or None


@_core
def browser_press_key(key: str, confirm_token: str | None = None, repeat: int = 1) -> dict:
    name, modifiers = _key(key)
    return k.browser_keypress(key=name, modifiers=modifiers, tab_id=_current_tab, confirm_token=confirm_token,
                              repeat=repeat)


@_core
def browser_type(target: str, text: str, element: str | None = None, submit: bool | None = None,
                 slowly: bool | None = None, confirm_token: str | None = None) -> dict:
    result = k.browser_type(text=text, mode="type" if slowly else "fill", tab_id=_current_tab, element=element,
                            confirm_token=None if submit else confirm_token, **_target(target))
    if not submit or k._failed(result):
        return result
    return k.browser_keypress(key="Enter", tab_id=_current_tab, element=element, confirm_token=confirm_token,
                              **_target(target))


@_core
def browser_navigate(url: str, confirm_token: str | None = None, tab_id: int | None = None) -> dict:
    return k.browser_navigate(url=url, tab_id=_current_tab if tab_id is None else tab_id,
                              confirm_token=confirm_token)


@_core
def browser_navigate_back(tab_id: int | None = None) -> dict:
    return k.browser_back(tab_id=_current_tab if tab_id is None else tab_id)


def _successful_static(entry: dict) -> bool:
    status = entry.get("responseStatus") or 0  # 0: unknown (cross-origin without Timing-Allow-Origin)
    return entry.get("initiator") in _STATIC and status < 400


@_core
def browser_network_requests(static: bool = False, filter: str | None = None, filename: str | None = None) -> dict:
    result = k._post("/api/capture-network", k._request_body({"op": "all"}, _current_tab))
    if k._failed(result):
        return result
    entries = [entry for entry in result.get("entries") or [] if static or not _successful_static(entry)]
    if filter:
        try:
            pattern = re.compile(filter)
        except re.error as e:
            return {"error": f"Invalid filter regex: {e}"}
        entries = [entry for entry in entries if pattern.search(entry.get("url") or "")]
    listed = [{"index": i, **entry} for i, entry in enumerate(entries, 1)]
    _requests[_current_tab] = listed
    if filename:
        lines = (f"{e['index']}. [{e.get('responseStatus') or '-'}] {e.get('initiator')} {e.get('url')}\n"
                 for e in listed)
        return {**_saved(filename, "".join(lines)), "count": len(listed)}
    return {"count": len(listed), "requests": listed}


@_core
def browser_network_request(index: int, part: str | None = None, filename: str | None = None) -> dict:
    listed = _requests.get(_current_tab)
    if not listed:
        return {"error": "No request list; call browser_network_requests first"}
    if not 1 <= index <= len(listed):
        return {"error": f"No request {index}; browser_network_requests listed {len(listed)}"}
    if part is not None:
        return _not_supported(f"browser_network_request part={part}",
                              "Kitsune reads requests from the page's resource timing, which has no "
                              "headers or bodies.")
    entry = listed[index - 1]
    if filename:
        return _saved(filename, json.dumps(entry, indent=2, ensure_ascii=False))
    return entry


@_core
def browser_run_code_unsafe(code: str | None = None, filename: str | None = None) -> dict:
    return _not_supported("browser_run_code_unsafe",
                          "Kitsune has no Playwright page; use browser_evaluate for page JavaScript.")


_IMAGE_SUFFIXES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}


@_core
def browser_take_screenshot(scale: str = "css", element: str | None = None, target: str | None = None,
                            type: str | None = None, filename: str | None = None, fullPage: bool | None = None):
    # scale: the browser captures at its own pixel ratio.
    if target is not None and fullPage:
        return {"error": "fullPage cannot be used with element screenshots"}
    image_type = type or (_IMAGE_SUFFIXES.get(Path(filename).suffix.lower()) if filename else None) or "png"
    if image_type not in {"png", "jpeg"}:
        return _not_supported(f"browser_take_screenshot type={image_type}",
                              "Use type png or jpeg, or a .png, .jpg or .jpeg filename.")
    return k.browser_screenshot(full_page=bool(fullPage), tab_id=_current_tab, element=element,
                                save_to=str(_path(filename)) if filename else None,
                                format=image_type, **_target(target))


# Inline snapshots are capped (#262); one saved to a file is whole. The cut falls between
# blocks such as product cards (extension getSnapshot), and its guidance names core tools only.
SNAPSHOT_LIMIT = 12000
_TRUNCATED_LINE = "- … truncated; browser_find searches the rest"
_TRUNCATED_HINT = (f"Snapshot truncated at {SNAPSHOT_LIMIT:,} characters, between blocks. browser_find searches "
                   "the whole page; pass a ref from it or from this snapshot as target to read that part "
                   "(\"e12 >> ..\" reads its container), or set filename to save the whole snapshot.")


@_core
def browser_snapshot(target: str | None = None, filename: str | None = None, depth: float | None = None,
                     boxes: bool | None = None) -> dict:
    body = k._request_body({} if filename else {"maxChars": SNAPSHOT_LIMIT}, _current_tab)
    if target is not None:
        body.update(_target(target))
    if depth is not None:
        if depth < 0 or depth != int(depth):
            return {"error": "depth must be a non-negative integer"}
        body["maxDepth"] = int(depth)
    if boxes:
        body["boxes"] = True
    result = k._post("/api/snapshot", body)
    if filename and not k._failed(result):
        return {**_saved(filename, result.get("snapshot") or ""),
                **{key: value for key, value in result.items() if key not in ("snapshot", "ok")}}
    if result.get("truncated") and "hint" in result and isinstance(result.get("snapshot"), str):
        # The extension's marker and hint name the full toolset's scope parameters.
        lines = result["snapshot"].split("\n")
        if lines[-1].startswith("- … truncated"):
            lines[-1] = _TRUNCATED_LINE
        result = {**result, "snapshot": "\n".join(lines), "hint": _TRUNCATED_HINT}
    return result


@_core
def browser_click(target: str, element: str | None = None, doubleClick: bool | None = None,
                  button: str | None = None, modifiers: list[str] | None = None,
                  confirm_token: str | None = None) -> dict:
    _clicked[_current_tab] = target
    selector, ref = _selector(target)
    body = {"selector": selector}
    if modifiers:
        body["modifiers"] = modifiers
    if doubleClick:
        if button not in (None, "left"):
            return {"error": "doubleClick supports only the left button"}
        return k._post_action("/api/double-click", k._request_body(body, _current_tab, ref, element, confirm_token))
    if button is not None:
        body["button"] = button
    return k._post_action("/api/click", k._request_body(body, _current_tab, ref, element, confirm_token))


@_core
def browser_drag(startTarget: str, endTarget: str, startElement: str | None = None,
                 endElement: str | None = None, confirm_token: str | None = None) -> dict:
    # The extension reads refs given as the from/to selectors.
    return k.browser_drag(from_selector=startTarget, to_selector=endTarget, tab_id=_current_tab,
                          element=startElement, confirm_token=confirm_token)


@_core
def browser_hover(target: str, element: str | None = None) -> dict:
    return k.browser_hover(tab_id=_current_tab, element=element, **_target(target))


def _select(target: str, values: list[str], element: str | None, confirm_token: str | None) -> dict:
    selector, ref = _selector(target)
    return k._post_action("/api/select-option", k._request_body(
        {"selector": selector, "values": values}, _current_tab, ref, element, confirm_token))


@_core
def browser_select_option(target: str, values: list[str], element: str | None = None,
                          confirm_token: str | None = None) -> dict:
    return _select(target, values, element, confirm_token)


@_core
def browser_tabs(action: str, index: float | None = None, url: str | None = None,
                 confirm_token: str | None = None) -> dict:
    global _current_tab
    if action == "list":
        return _tab_list()
    if action == "new":
        # The new tab's index replaces the bridge's tabId, which no core tool takes.
        result = k.browser_tabs("new", url=url, wait=url is not None, confirm_token=confirm_token)
        if not isinstance(result.get("tabId"), int):
            return result
        _current_tab = result["tabId"]
        result = _with_tab_list({key: value for key, value in result.items() if key != "tabId"})
        index = [tab["index"] for tab in result.get("tabs", ()) if tab.get("current")]
        return {**result, "index": index[0]} if index else result
    if action == "select":
        if index is None:
            return {"error": "index is required for action=select"}
        tab_id, error = _tab_at(index)
        if error:
            return error
        _current_tab = tab_id
        return _with_tab_list({"ok": True, "index": int(index)})
    if action == "close":
        if index is not None:
            tab_id, error = _tab_at(index)
            if error:
                return error
        elif _current_tab is not None:
            tab_id = _current_tab
        else:
            result = _tabs()
            if k._failed(result):
                return result
            active = [tab["id"] for tab in result.get("tabs") or [] if tab.get("active")]
            if len(active) != 1:
                return {"error": "Several windows have an active tab; pass index", "code": "ambiguous_target",
                        "retryable": False}
            tab_id = active[0]
        result = k.browser_tabs("close", tab_id=tab_id, confirm_token=confirm_token)
        if not k._failed(result):
            _clicked.pop(tab_id, None)
            _requests.pop(tab_id, None)
            if tab_id == _current_tab:
                _current_tab = None
            return _with_tab_list(result)
        return result
    return {"error": "action must be list, new, close, or select"}


@_core
def browser_wait_for(time: float | None = None, text: str | None = None, textGone: str | None = None) -> dict:
    if time is None and text is None and textGone is None:
        return {"error": "Either time, text or textGone must be provided"}
    result = {"ok": True}
    if time is not None:
        result = k.browser_wait_for(time=min(time, 30))
        if k._failed(result):
            return result
    if textGone is not None:
        result = k.browser_wait_for(text_gone=textGone, tab_id=_current_tab)
        if k._failed(result):
            return result
    if text is not None:
        result = k.browser_wait_for(text=text, tab_id=_current_tab)
    return result
