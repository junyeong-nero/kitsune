"""
Kitsune bridge daemon.
The MCP server sends commands over HTTP (127.0.0.1:47651, bearer token);
the browser extension connects over WebSocket (127.0.0.1:47652) and runs them.
The MCP server starts this daemon on demand, so it is shared by every agent.

Usage: python -m kitsune.bridge

------------------------------------------------------------------------
AI AGENT NOTICE -- READ BEFORE WRITING SCRAPING BATCH SEQUENCES
------------------------------------------------------------------------
Before writing any batch commands that scrape a known website, check:
    docs/scraping-playbooks/README.md   (relative to repo root)
Playbooks exist for sites that required non-obvious workarounds:
  * amazon.md       -- bot detection / scroll-before-extract pattern
  * bestbuy.md      -- lazy loading / window.scrollTo fix / 2026 selectors
  * newegg.md       -- silent category redirects / use /p/pl?d= search URL
  * bhphotovideo.md -- CSS Modules hashed classes / wildcard selector pattern
Using a playbook avoids re-discovering the same failures from scratch.
If you solve a new site, add a playbook so future agents benefit.
------------------------------------------------------------------------
"""

import argparse
import asyncio
import fcntl
import signal
import json
import base64
import math
import os
import shutil
import subprocess
import sys
import time
import ctypes
from contextlib import asynccontextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Lock, Thread
from datetime import datetime

import hashlib
import hmac
import secrets
import stat

# No silent auto-install: a bridge that controls a logged-in browser should
# not run pip on its own.
try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve
except ImportError:
    sys.exit("websockets is not installed. Reinstall kitsune with its dependencies.")

from . import bridge_control
from .version import BRIDGE_BUILD, PACKAGE_VERSION
from .waits import actionability_wait_seconds, batch_wait_seconds

# ── Config ──
BRIDGE_VERSION = PACKAGE_VERSION
API_VERSION = "1"
HTTP_PORT = int(os.environ.get("KITSUNE_HTTP_PORT", "47651"))
WS_PORT = int(os.environ.get("KITSUNE_WS_PORT", "47652"))
# Opt-in idle shutdown in seconds (0 = never). The MCP server passes its
# environment to the bridge it spawns, so clients can set this in their config.
IDLE_TIMEOUT_ENV = "KITSUNE_BRIDGE_IDLE_TIMEOUT"
IDLE_POLL_SECONDS = 1.0
# Everything the bridge writes (token, cookies from saved sessions,
# screenshots, logs) stays in one user-only directory.
DATA_DIR = os.environ.get("KITSUNE_HOME", os.path.join(os.path.expanduser("~"), ".kitsune"))
os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
SCREENSHOT_DIR = os.path.join(DATA_DIR, "screenshots")
WORKFLOW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows")

# ── Auth ──
# Any web page open in the browser can send requests to 127.0.0.1, so the
# HTTP API requires a bearer token and the WebSocket only accepts extension
# origins. The token lives in a user-only file that the MCP server also reads.
TOKEN_FILE = os.path.join(DATA_DIR, "token")
ALLOWED_HOSTS = {f"127.0.0.1:{HTTP_PORT}", f"localhost:{HTTP_PORT}"}
ALLOWED_WS_ORIGIN_PREFIX = "moz-extension://"
# Paths that a browser tab navigates to directly and cannot attach a token.
UNAUTHENTICATED_GET_PATHS = {"/view"}


def _load_or_create_token():
    env_token = os.environ.get("KITSUNE_TOKEN")
    if env_token:
        return env_token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    return token


AUTH_TOKEN = _load_or_create_token()

# ── State ──
extension_ws = None  # WebSocket connection to extension
extension_ws_generation = 0
extension_info = {}  # {"version", "build"} from the extension's hello message
pending_commands = {}  # id -> {"future": asyncio.Future, "ws": websocket, ...}
command_counter = 0
_ws_send_lock = None
_global_command_lock = None
_tab_command_locks = {}
_scheduler_gate = None
_scheduler_active = {}
_scheduler_queued = {}

# ── Cache ──
import re as _re
_cache = {}  # key -> (timestamp, data)
CACHE_TTL = 5  # seconds

# ── Keep-alive (tab unloader defense) ──
# Zen aggressively unloads inactive tabs. For reliable parallel multi-tab work
# we run a background pinger per tab the agent has asked us to keep warm.
_keep_alive_tasks = {}  # tab_id -> asyncio.Task

# ── 1.4.0 orchestration / state ──
_tags = {}  # tag name -> tab_id
_tab_pool = {  # tab id pool for fast-acquire workflows
    "size": 0,
    "available": [],   # list of tab ids
    "in_use": set(),   # tab ids currently checked out
    "url": "about:blank",
}
_audit_log = []  # list of {ts, action, params, ok, error, ms}
_AUDIT_MAX = 2000
_log_buffer = []  # mirrors bridge stdout lines
_LOG_MAX = 5000
# Ask-before-act is opt-in. askUrls are payment-like URL patterns it guards (case-insensitive).
DEFAULT_ASK_URLS = [r"checkout", r"payment", r"billing", r"/pay\b", r"purchase", r"/order"]
_policy = {"allow": [], "deny": [], "readonly": False,
           "ask": False, "askUrls": list(DEFAULT_ASK_URLS), "askLocked": False}
_sessions_dir = os.path.join(DATA_DIR, "sessions")

def _cache_key(action, params):
    return action + ":" + json.dumps(params, sort_keys=True)

def _cache_get(key):
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < CACHE_TTL:
        return entry[1]
    if entry:
        del _cache[key]
    return None

def _cache_set(key, data):
    _cache[key] = (time.time(), data)
    if len(_cache) > 100:
        cutoff = time.time() - CACHE_TTL
        for k in [k for k, (t, _) in _cache.items() if t < cutoff]:
            del _cache[k]


def _cache_clear(reason=None):
    """Clear short-lived read cache after browser/page mutations."""
    if _cache:
        _cache.clear()


def _focus_window_by_title(title):
    """Best-effort foreground focus for callers that explicitly ask for it."""
    if not sys.platform.startswith("win") or not title:
        return False

    user32 = ctypes.windll.user32
    matches = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        text = buf.value
        if title in text or text in title:
            matches.append(hwnd)
        return True

    user32.EnumWindows(enum_proc, 0)
    if not matches:
        return False

    hwnd = matches[0]
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    return user32.GetForegroundWindow() == hwnd


# Supported Firefox-family browsers; env vars take comma lists for other builds.
_DEFAULT_BROWSER_WINDOW_TITLES = ("Zen Browser", "Mozilla Firefox", "LibreWolf")
_DEFAULT_BROWSER_PROCESSES = ("zen", "firefox", "librewolf")

# Process names arrive as argv so configured names are never spliced into the script.
_MAC_FOCUS_SCRIPT = """on run argv
  tell application "System Events"
    repeat with procName in argv
      set procName to procName as text
      if exists process procName then
        set frontmost of process procName to true
        return procName
      end if
    end repeat
  end tell
  return ""
end run"""


def _env_names(name, default):
    names = [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]
    return names or list(default)


def _focus_mac_process(names):
    try:
        result = subprocess.run(
            ["osascript", "-e", _MAC_FOCUS_SCRIPT, *names],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0 or not result.stdout.strip():
        return False
    time.sleep(0.05)
    return True


def _focus_browser_window(title="", process=""):
    """Best-effort browser focus: window title on Windows, process name on macOS."""
    if sys.platform.startswith("win"):
        titles = [title] if title else _env_names("KITSUNE_BROWSER_WINDOW_TITLE", _DEFAULT_BROWSER_WINDOW_TITLES)
        return any(_focus_window_by_title(t) for t in titles)
    if sys.platform == "darwin":
        processes = [process] if process else _env_names("KITSUNE_BROWSER_PROCESS", _DEFAULT_BROWSER_PROCESSES)
        return _focus_mac_process(processes)
    return False


_NO_SCHEDULE_ACTIONS = {"ping", "getTabs", "getWindows"}
_GLOBAL_SCOPE_ACTIONS = {
    "screenshot", "switchTab", "newTab", "closeTab", "trustedClick",
    "reloadExtension", "createWindow", "closeWindow", "focusWindow",
    "moveTab", "detachTab", "elementScreenshot", "fullPageScreenshot",
    "cookies", "clipboard", "downloads", "clearBrowsingData", "intercept",
}


_API_ALIASES = {
    "/api/pageInfo": "/api/page-info",
    "/api/pageText": "/api/page-text",
    "/api/pageTextByTabId": "/api/page-text-by-tab-id",
    "/api/executeJS": "/api/js",
    "/api/trustedClick": "/api/trusted-click",
    "/api/setEditableContent": "/api/set-editable-content",
    "/api/clearHighlight": "/api/clear-highlight",
    "/api/closeTab": "/api/close-tab",
    "/api/switchTab": "/api/switch-tab",
    "/api/newTab": "/api/new-tab",
    "/api/waitForElement": "/api/wait-for-element",
    "/api/waitForResult": "/api/wait-for-result",
    "/api/wakeTab": "/api/wake-tab",
    "/api/keepAlive": "/api/keep-alive",
    "/api/keepAliveStop": "/api/keep-alive-stop",
    "/api/reloadExtension": "/api/reload-extension",
    "/api/structuredData": "/api/structured-data",
    "/api/computedStyle": "/api/computed-style",
    "/api/selectOption": "/api/select-option",
    "/api/doubleClick": "/api/double-click",
    "/api/submitForm": "/api/submit-form",
    "/api/formFill": "/api/form-fill",
    "/api/waitForUrl": "/api/wait-for-url",
    "/api/waitForTitle": "/api/wait-for-title",
    "/api/waitForNetworkIdle": "/api/wait-for-network-idle",
    "/api/captureNetwork": "/api/capture-network",
    "/api/watchConsole": "/api/watch-console",
    "/api/consoleLogs": "/api/console-logs",
    "/api/explainSelector": "/api/explain-selector",
    "/api/fullPageMetrics": "/api/full-page-metrics",
    "/api/pinTab": "/api/pin-tab",
    "/api/muteTab": "/api/mute-tab",
    "/api/duplicateTab": "/api/duplicate-tab",
    "/api/reloadTab": "/api/reload-tab",
    "/api/getZoom": "/api/get-zoom",
    "/api/setZoom": "/api/set-zoom",
    "/api/createWindow": "/api/create-window",
    "/api/closeWindow": "/api/close-window",
    "/api/focusWindow": "/api/focus-window",
    "/api/moveTab": "/api/move-tab",
    "/api/detachTab": "/api/detach-tab",
    "/api/elementScreenshot": "/api/element-screenshot",
    "/api/fullPageScreenshot": "/api/full-page-screenshot",
    "/api/clearBrowsingData": "/api/clear-browsing-data",
    "/api/clickAndWaitNavigation": "/api/click-and-wait-navigation",
    "/api/syncBarrier": "/api/sync-barrier",
    "/api/tagTab": "/api/tag-tab",
    "/api/resolveTag": "/api/resolve-tag",
    "/api/listTags": "/api/list-tags",
    "/api/untagTab": "/api/untag-tab",
    "/api/tabPool": "/api/tab-pool",
    "/api/poolAcquire": "/api/pool-acquire",
    "/api/poolRelease": "/api/pool-release",
    "/api/saveSession": "/api/save-session",
    "/api/loadSession": "/api/load-session",
    "/api/listSessions": "/api/list-sessions",
    "/api/deleteSession": "/api/delete-session",
    "/api/setPolicy": "/api/set-policy",
    "/api/getPolicy": "/api/get-policy",
}


class _SchedulerGate:
    def __init__(self):
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    async def acquire_read(self):
        async with self._cond:
            while self._writer or self._waiting_writers > 0:
                await self._cond.wait()
            self._readers += 1

    async def release_read(self):
        async with self._cond:
            self._readers = max(0, self._readers - 1)
            if self._readers == 0:
                self._cond.notify_all()

    async def acquire_write(self):
        async with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers > 0:
                    await self._cond.wait()
                self._writer = True
            finally:
                self._waiting_writers = max(0, self._waiting_writers - 1)

    async def release_write(self):
        async with self._cond:
            self._writer = False
            self._cond.notify_all()

    def snapshot(self):
        return {
            "active_tab_scopes": self._readers,
            "global_active": self._writer,
            "global_waiting": self._waiting_writers,
        }


def _coerce_tab_id(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scheduler_snapshot():
    queued_scopes = {k: v for k, v in _scheduler_queued.items() if v > 0}
    gate = _scheduler_gate.snapshot() if _scheduler_gate is not None else {
        "active_tab_scopes": 0,
        "global_active": False,
        "global_waiting": 0,
    }
    return {
        "mode": "threaded-http/per-tab-queue",
        "pending_commands": len(pending_commands),
        "queued_commands": sum(queued_scopes.values()),
        "active_scopes": dict(_scheduler_active),
        "queued_scopes": queued_scopes,
        "gate": gate,
        "tab_locks": sorted(_tab_command_locks.keys()),
    }


def _get_scheduler_gate():
    global _scheduler_gate
    if _scheduler_gate is None:
        _scheduler_gate = _SchedulerGate()
    return _scheduler_gate


@asynccontextmanager
async def _scheduler_gate_lease(scope):
    gate = _get_scheduler_gate()
    if scope[0] == "global":
        await gate.acquire_write()
        try:
            yield
        finally:
            await gate.release_write()
    else:
        await gate.acquire_read()
        try:
            yield
        finally:
            await gate.release_read()


def _get_ws_send_lock():
    global _ws_send_lock
    if _ws_send_lock is None:
        _ws_send_lock = asyncio.Lock()
    return _ws_send_lock


def _get_scope_lock(scope):
    global _global_command_lock
    if scope[0] == "global":
        if _global_command_lock is None:
            _global_command_lock = asyncio.Lock()
        return _global_command_lock

    tab_id = scope[1]
    lock = _tab_command_locks.get(tab_id)
    if lock is None:
        lock = asyncio.Lock()
        _tab_command_locks[tab_id] = lock
    return lock


def _command_scope(action, params):
    """Return a scheduler scope for an extension command.

    Tab-scoped commands may overlap across different tabs. Commands that rely
    on the active/focused tab, browser-global state, or capture APIs are
    serialized through the global scope.
    """
    if action in _NO_SCHEDULE_ACTIONS:
        return None
    if action in _GLOBAL_SCOPE_ACTIONS:
        return ("global", action)

    tab_id = _coerce_tab_id((params or {}).get("tabId"))
    if tab_id is None:
        return ("global", action)
    return ("tab", tab_id)


async def _run_in_command_scope(action, params, coro_factory):
    scope = _command_scope(action, params)
    if scope is None:
        return await coro_factory()

    lock = _get_scope_lock(scope)
    scope_name = f"{scope[0]}:{scope[1]}"
    queued = True
    _scheduler_queued[scope_name] = _scheduler_queued.get(scope_name, 0) + 1
    try:
        async with lock:
            async with _scheduler_gate_lease(scope):
                _scheduler_queued[scope_name] = max(0, _scheduler_queued.get(scope_name, 1) - 1)
                if _scheduler_queued[scope_name] == 0:
                    _scheduler_queued.pop(scope_name, None)
                queued = False
                _scheduler_active[scope_name] = action
                try:
                    return await coro_factory()
                finally:
                    _scheduler_active.pop(scope_name, None)
    finally:
        if queued:
            _scheduler_queued[scope_name] = max(0, _scheduler_queued.get(scope_name, 1) - 1)
            if _scheduler_queued[scope_name] == 0:
                _scheduler_queued.pop(scope_name, None)


def _cleanup_tab_scheduler_state(tab_id):
    tab_id = _coerce_tab_id(tab_id)
    if tab_id is None:
        return
    scope_name = f"tab:{tab_id}"
    lock = _tab_command_locks.get(tab_id)
    if (
        lock is not None
        and not lock.locked()
        and scope_name not in _scheduler_active
        and _scheduler_queued.get(scope_name, 0) == 0
    ):
        _tab_command_locks.pop(tab_id, None)


_MAC_ACCESSIBILITY_HELP = (
    "grant Accessibility permission to the app running the Kitsune bridge "
    "(System Settings > Privacy & Security > Accessibility), then restart the bridge"
)


def _mac_accessibility_trusted():
    """AXIsProcessTrusted() via ctypes; None when it cannot be checked."""
    try:
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except (OSError, AttributeError):
        return None


def _cliclick_coord(value):
    # cliclick reads a leading "-" as relative; "=" marks an absolute negative value.
    value = int(round(value))
    return f"={value}" if value < 0 else str(value)


def _mac_click_screen(x, y):
    # macOS drops posted mouse events from untrusted processes without an error.
    if _mac_accessibility_trusted() is False:
        return {"error": f"trustedClick needs macOS Accessibility permission: {_MAC_ACCESSIBILITY_HELP}"}

    try:
        import Quartz
    except ImportError:
        Quartz = None
    if Quartz is not None:
        point = (float(x), float(y))
        for event_type, pause in (
            (Quartz.kCGEventMouseMoved, 0.05),
            (Quartz.kCGEventLeftMouseDown, 0.03),
            (Quartz.kCGEventLeftMouseUp, 0),
        ):
            event = Quartz.CGEventCreateMouseEvent(None, event_type, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            if pause:
                time.sleep(pause)
        return {"ok": True, "backend": "quartz"}

    cliclick = shutil.which("cliclick")
    if cliclick:
        try:
            result = subprocess.run(
                [cliclick, f"c:{_cliclick_coord(x)},{_cliclick_coord(y)}"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return {"error": f"cliclick failed: {e}"}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit code {result.returncode}"
            return {"error": f"cliclick failed: {detail}"}
        return {"ok": True, "backend": "cliclick"}

    return {
        "error": (
            "trustedClick on macOS needs pyobjc Quartz (run Kitsune with "
            "uvx --from 'kitsune-browser-mcp[macos]' kitsune) or cliclick on PATH "
            f"(brew install cliclick); also {_MAC_ACCESSIBILITY_HELP}"
        )
    }


def _native_click_screen(x, y):
    if sys.platform == "darwin":
        return _mac_click_screen(x, y)
    if not sys.platform.startswith("win"):
        return {"error": "trustedClick is only implemented on Windows and macOS"}

    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(round(x)), int(round(y)))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.03)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    return {"ok": True, "backend": "win32"}


async def trusted_click(params):
    """Use a real OS-level mouse click at a browser viewport coordinate."""
    selector = params.get("selector") or ""
    coords = params.get("coords") or {}
    coordinate_space = params.get("coordinateSpace") or params.get("space") or "viewport"
    focus = params.get("focus", False)
    x = params.get("x", coords.get("x"))
    y = params.get("y", coords.get("y"))

    if not selector and (x is None or y is None):
        return {"error": "trustedClick requires selector or x/y coordinates"}

    title = params.get("title") or ""
    process = params.get("process") or ""

    if coordinate_space == "screen":
        focused = _focus_browser_window(title, process) if focus else False
        clicked = _native_click_screen(x, y)
        if "error" in clicked:
            return clicked
        return {
            "ok": True,
            "trusted": True,
            "coordinateSpace": "screen",
            "screenX": x,
            "screenY": y,
            "focused": focused,
            "focusRequested": bool(focus),
            "clickBackend": clicked.get("backend"),
        }

    point_params = {"selector": selector, "x": x, "y": y}
    if params.get("tabId") is not None:
        point_params["tabId"] = params.get("tabId")
    point = await send_to_extension("screenPoint", point_params)

    if "error" in point:
        return point

    focused = _focus_browser_window(title or point.get("title", ""), process) if focus else False
    clicked = _native_click_screen(point["screenX"], point["screenY"])
    if "error" in clicked:
        return clicked

    return {
        "ok": True,
        "trusted": True,
        "screenX": point["screenX"],
        "screenY": point["screenY"],
        "viewportX": point["viewportX"],
        "viewportY": point["viewportY"],
        "title": point.get("title", ""),
        "focused": focused,
        "focusRequested": bool(focus),
        "clickBackend": clicked.get("backend"),
    }


def _log(msg):
    """Tee print() to in-memory ring buffer so /api/logs can read it back."""
    line = f"[{now()}] {msg}"
    print(line)
    _log_buffer.append(line)
    if len(_log_buffer) > _LOG_MAX:
        del _log_buffer[: len(_log_buffer) - _LOG_MAX]


AUDIT_REDACTED = "[redacted]"
# Params holding typed text or stored secrets (cookie, storage and clipboard
# contents); selectors, refs, tabId, op, name, url and key stay.
_AUDIT_SECRET_PARAMS = {
    "type": ("text",),
    "fill": ("value",),
    "setEditableContent": ("value",),
    "keypress": ("text",),
    "cookies": ("value",),
    "storageOp": ("value",),
    "clipboard": ("text",),
}


def _audit_params(action, params):
    """Copy params for the audit log without JS bodies, typed values or secrets.

    Nested commands (batch, parallel, broadcast, retry, if/while/try, sequence)
    are dicts with their own "action" and get the same treatment.
    """
    def scrub(value):
        if isinstance(value, dict):
            if isinstance(value.get("action"), str):
                return _audit_params(value["action"], value)
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    secret = _AUDIT_SECRET_PARAMS.get(action, ())
    out = {}
    for k, v in (params or {}).items():
        if k == "code":  # don't log JS bodies
            continue
        if k in secret:
            out[k] = AUDIT_REDACTED
        elif action == "formFill" and k == "fields":
            # Keys are selectors/labels and stay; every value is typed content.
            out[k] = {f: AUDIT_REDACTED for f in v} if isinstance(v, dict) else AUDIT_REDACTED
        else:
            out[k] = scrub(v)
    return out


def _audit(action, params, ok, error, ms):
    _audit_log.append({
        "ts": int(time.time() * 1000),
        "action": action,
        "params": _audit_params(action, params),
        "ok": bool(ok),
        "error": error,
        "ms": int(ms),
    })
    if len(_audit_log) > _AUDIT_MAX:
        del _audit_log[: len(_audit_log) - _AUDIT_MAX]


WRITE_ACTIONS = {
    "click", "type", "setEditableContent", "scroll", "hover", "fill",
    "navigate", "newTab", "closeTab", "switchTab", "executeJS", "trustedClick",
    "selectOption", "checkBox", "keypress", "doubleClick", "submitForm",
    "formFill", "drag", "cookies", "clipboard", "downloads",
    "clearBrowsingData", "intercept", "clickAndWaitNavigation",
    "pinTab", "muteTab", "duplicateTab", "reloadTabBrowser", "goBack", "goForward",
    "setZoom", "createWindow", "closeWindow", "moveTab", "detachTab",
    "storageOp", "reloadExtension", "wakeTab", "highlight", "clearHighlight",
    "uploadFile", "handleDialog",
}


# Repeating these after an unknown outcome could duplicate a side effect (a submit,
# message, payment or deletion), so their timeouts and disconnects are not retryable.
_REPEAT_UNSAFE_ACTIONS = WRITE_ACTIONS | {"uploadCommit", "focusElement", "blurElement"}
# Nothing reached the page, but repeating cannot succeed until the tab navigates.
_PERSISTENT_FAILURE_CODES = {"error_page"}


def _check_policy(action, params):
    """Return error dict if action/url violates current policy, else None."""
    if _policy["readonly"] and action in WRITE_ACTIONS:
        # Read-only ops on cookies/storage/etc. are still allowed; tighten if needed.
        sub_op = (params or {}).get("op") if isinstance(params, dict) else None
        if action in ("cookies", "clipboard", "storageOp", "downloads") and sub_op in ("get", "read", "list", "snapshot"):
            return None
        return {"error": f"Read-only mode is active; refusing action: {action}"}
    url = (params or {}).get("url") if isinstance(params, dict) else None
    if url:
        for pat in _policy.get("deny", []):
            try:
                if _re.search(pat, url):
                    return {"error": f"URL denied by policy ({pat}): {url}"}
            except _re.error:
                pass
        allow = _policy.get("allow", [])
        if allow:
            ok = False
            for pat in allow:
                try:
                    if _re.search(pat, url):
                        ok = True
                        break
                except _re.error:
                    pass
            if not ok:
                return {"error": f"URL not in allowlist: {url}"}
    return None


# ── Ask-before-act (docs/api-reference.md#ask-before-act) ──
# Held actions do not run: they return confirmation_required with a token bound to the exact
# action, parameters and tab. The token goes back to the agent, so it enforces an explicit second
# step, not proof that a human approved.
ASK_BEFORE_ACT_ENV = "KITSUNE_ASK_BEFORE_ACT"
CONFIRM_TOKEN_TTL = 120
_UNKNOWN_OUTCOME_TTL = 600
_ASK_JS_ACTIONS = {"executeJS", "waitForResult", "syncBarrier", "if", "while"}
_ASK_COOKIE_ACTIONS = {"cookies", "saveSession", "loadSession"}
_ASK_NAVIGATION_ACTIONS = {"navigate", "newTab", "createWindow"}
_ASK_CLICK_ACTIONS = {"click", "doubleClick", "trustedClick", "clickAndWaitNavigation"}
# Page actions held on payment-like pages; they target one tab, which the token is bound to.
_ASK_PAGE_ACTIONS = _ASK_CLICK_ACTIONS | {
    "keypress", "type", "setEditableContent", "fill", "formFill", "selectOption", "checkBox",
    "drag", "submitForm", "uploadFile", "handleDialog", "storageOp", "executeJS", "waitForResult",
}
_ASK_TAB_ACTIONS = _ASK_PAGE_ACTIONS | {"navigate"}
_confirm_tokens = {}     # token -> {"binding", "expires", "used"}
_unknown_outcomes = {}   # outcome key -> expiry, for writes that ended retryable: false


def _ask_url_match(url):
    for pat in _policy.get("askUrls", []):
        try:
            if url and _re.search(pat, url, _re.I):
                return pat
        except _re.error:
            pass
    return None


def _outcome_key(action, params):
    payload = {"action": action, "params": {k: v for k, v in (params or {}).items() if k != "confirmToken"}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _confirmation_binding(action, params, tab_id):
    return _outcome_key(action, {**(params or {}), "__tab": tab_id})


def _remember_outcome(action, params, result):
    """Under ask mode, remember writes whose outcome is unknown so repeating them is held."""
    if (not _policy["ask"] or not isinstance(result, dict) or result.get("retryable") is not False
            or result.get("code") in _PERSISTENT_FAILURE_CODES):
        return
    now_s = time.monotonic()
    for key in [k for k, exp in _unknown_outcomes.items() if exp < now_s]:
        del _unknown_outcomes[key]
    _unknown_outcomes[_outcome_key(action, params)] = now_s + _UNKNOWN_OUTCOME_TTL
    while len(_unknown_outcomes) > 200:
        del _unknown_outcomes[next(iter(_unknown_outcomes))]


def _issue_confirm_token(binding):
    now_s = time.monotonic()
    for token in [t for t, entry in _confirm_tokens.items() if entry["expires"] < now_s]:
        del _confirm_tokens[token]
    while len(_confirm_tokens) >= 100:
        del _confirm_tokens[next(iter(_confirm_tokens))]
    token = secrets.token_urlsafe(24)
    _confirm_tokens[token] = {"binding": binding, "expires": now_s + CONFIRM_TOKEN_TTL, "used": False}
    return token


def _spend_confirm_token(token, binding):
    """Burn a presented token; return None if it confirms binding, else why it was rejected."""
    entry = _confirm_tokens.get(token) if isinstance(token, str) else None
    if entry is None:
        return "unknown"
    if entry["used"]:
        return "used"
    entry["used"] = True
    if entry["expires"] < time.monotonic():
        return "expired"
    if entry["binding"] != binding:
        return "mismatch"
    return None


async def _describe_target(params, inspect):
    target = {key: params[key] for key in ("tabId", "selector", "ref", "key") if params.get(key) is not None}
    coords = params.get("coords") or ({"x": params["x"], "y": params["y"]} if "x" in params and "y" in params else None)
    if coords and not target.get("selector") and not target.get("ref"):
        target["coords"] = coords
    return await send_to_extension("describeTarget", {**target, "inspect": inspect}, timeout=10)


async def _held_reason(action, params):
    """Return (reason, description, tab_id, url) when ask mode holds this action, else None."""
    reason = description = None
    url = params.get("url") if action in _ASK_NAVIGATION_ACTIONS else None
    if action in _ASK_JS_ACTIONS:
        reason, description = "javascript", "runs agent-supplied JavaScript in the page"
    elif action in _ASK_COOKIE_ACTIONS:
        reason, description = "cookies", "reads or writes browser cookies"
    elif action == "submitForm":
        reason, description = "submit", "submits a form"
    elif _ask_url_match(url):
        reason, description = "payment_url", f"opens a payment-like URL ({_ask_url_match(url)}): {url}"
    elif _outcome_key(action, params) in _unknown_outcomes:
        reason, description = "unknown_outcome", "repeats a write whose previous outcome is unknown"

    tab_id = url_now = None
    if action in _ASK_TAB_ACTIONS:
        submit_key = action == "keypress" and params.get("key") == "Enter"
        screen = action == "trustedClick" and (params.get("coordinateSpace") or params.get("space")) == "screen"
        inspect = reason is None and (action in _ASK_CLICK_ACTIONS or submit_key) and not screen
        described = await _describe_target(params, inspect)
        if not isinstance(described, dict) or described.get("error"):
            # Fail closed: an action that cannot be inspected is held.
            error = described.get("error") if isinstance(described, dict) else described
            return (reason or "uninspectable_target",
                    description or f"acts on a target that could not be inspected ({error})",
                    _coerce_tab_id(params.get("tabId")), None)
        tab_id, url_now = described.get("tabId"), described.get("url")
        target = described.get("target") or {}
        if reason is None and action in _ASK_PAGE_ACTIONS and _ask_url_match(url_now):
            reason, description = "payment_page", f"acts on a payment-like page ({_ask_url_match(url_now)})"
        elif reason is None and screen:
            reason, description = "uninspectable_target", "clicks screen coordinates that cannot be inspected"
        elif reason is None and target.get("submits"):
            what = "presses Enter in a form field" if submit_key else "clicks a submit control"
            label = target.get("text") or target.get("tag") or ""
            reason, description = "submit_control", f"{what} ({label})" if label else what
    if reason is None:
        return None
    return reason, description, tab_id, url_now or url


async def _ask_before_act(action, params, *, direct):
    """Return a confirmation_required error when ask mode holds this action, else None.

    Removes confirmToken from params. Tokens are only honored on direct calls, so batch,
    retry, parallel, broadcast and workflows never confirm an action.
    """
    token = params.pop("confirmToken", None) if isinstance(params, dict) else None
    if not _policy["ask"]:
        return None
    held = await _held_reason(action, params)
    if held is None:
        return None
    reason, description, tab_id, url = held
    binding = _confirmation_binding(action, params, tab_id)
    rejected = None
    if token is not None:
        rejected = _spend_confirm_token(token, binding)
        if rejected is None and not direct:
            rejected = "not_direct"
        if rejected is None:
            _unknown_outcomes.pop(_outcome_key(action, params), None)
            _audit(action, {**params, "confirmation": "accepted", "reason": reason}, True, None, 0)
            return None
    _audit(action, {**params, "confirmation": "required", "reason": reason}, False, "confirmation_required", 0)
    state = {"confirmToken": _issue_confirm_token(binding), "expiresInSeconds": CONFIRM_TOKEN_TTL,
             "action": action, "reason": reason, "description": description,
             "tabId": tab_id, "url": url, "params": dict(params)}
    if rejected:
        state["tokenRejected"] = rejected
    return _action_error(
        f"Confirmation required: {description}", "confirmation_required", retryable=True,
        hint=("Nothing ran. Ask the user to approve this exact action, then repeat the identical call "
              f"with confirmToken (MCP: confirm_token) within {CONFIRM_TOKEN_TTL}s. The token works once, "
              "only for this action, parameters and tab, and only on a direct tool call (never inside "
              "batch, retry, parallel, broadcast or workflows)."),
        **state,
    )


def _invalid_ask_policy(body):
    """Return why set-policy's ask/askUrls are invalid, else None; nothing is coerced."""
    if "ask" in body and not isinstance(body["ask"], bool):
        return "ask must be true or false"
    if "askUrls" in body:
        urls = body["askUrls"]
        if not isinstance(urls, list):
            return "askUrls must be a list of regular expressions"
        for pat in urls:
            if not isinstance(pat, str) or not pat:
                return f"askUrls entries must be non-empty strings: {pat!r}"
            try:
                _re.compile(pat)
            except _re.error as exc:
                return f"askUrls entry {pat!r} is not a valid regular expression: {exc}"
    return None


def _is_held(result):
    return isinstance(result, dict) and result.get("code") == "confirmation_required"


def _invalidate_cache_for_action(action):
    if action in WRITE_ACTIONS or action in ("elementScreenshot", "fullPageScreenshot"):
        _cache_clear(action)


# ── Orchestration: broadcast and syncBarrier ──

async def _broadcast_async(tab_ids, command, timeout=30):
    """Send the same command (minus tabId) to each tab in parallel."""
    if not tab_ids:
        return {"error": "broadcast requires at least one tabId"}
    action = command.get("action")
    if not action:
        return {"error": "broadcast command needs an action"}
    base_params = {k: v for k, v in command.items() if k != "action"}

    async def one(tid):
        # Route through the same logic batch uses so behavior is identical.
        cmd = {**base_params, "action": action, "tabId": tid}
        try:
            res = await asyncio.wait_for(run_command(cmd), timeout=timeout)
            return tid, res
        except asyncio.TimeoutError:
            return tid, {"error": f"timed out after {timeout}s"}

    results = await asyncio.gather(*[one(t) for t in tab_ids])
    return {"results": {str(t): r for t, r in results}}


async def _sync_barrier_async(tab_ids, predicate, timeout=30000, poll_interval=300):
    """Wait until a JS predicate returns truthy on every listed tab.
    Returns per-tab status and overall ok flag."""
    if not tab_ids:
        return {"error": "syncBarrier requires at least one tabId"}
    deadline = time.monotonic() + (timeout / 1000)
    last = {}
    while time.monotonic() < deadline:
        all_pass = True
        for tid in tab_ids:
            r = await send_to_extension("executeJS", {"tabId": tid, "code": predicate}, timeout=10)
            if isinstance(r, dict) and r.get("devBuildRequired"):
                return {"ok": False, **r}
            last[tid] = r
            val = (r or {}).get("result")
            if val in (None, "", "0", "false", "null", "undefined"):
                all_pass = False
        if all_pass:
            return {"ok": True, "results": last}
        await asyncio.sleep(poll_interval / 1000)
    return {"ok": False, "results": last, "error": "syncBarrier timed out"}


# ── Tab pool ──

async def _ensure_tab_pool(target_size, url):
    """Grow or shrink the pool to target_size warm tabs."""
    while len(_tab_pool["available"]) + len(_tab_pool["in_use"]) < target_size:
        r = await send_to_extension("newTab", {"url": url})
        tid = r.get("tabId") if isinstance(r, dict) else None
        if tid:
            _tab_pool["available"].append(tid)
        else:
            return {"error": "failed to create pool tab: " + str(r)}
    # Shrink: close extra available tabs
    while len(_tab_pool["available"]) + len(_tab_pool["in_use"]) > target_size and _tab_pool["available"]:
        tid = _tab_pool["available"].pop()
        try:
            await send_to_extension("closeTab", {"tabId": tid})
        except Exception:
            pass
    _tab_pool["size"] = target_size
    _tab_pool["url"] = url
    return _pool_status()


def _pool_status():
    return {
        "size": _tab_pool["size"],
        "available": list(_tab_pool["available"]),
        "in_use": list(_tab_pool["in_use"]),
        "url": _tab_pool["url"],
    }


# ── Session save/load (cookies + localStorage) ──

def _session_path(name):
    safe = _re.sub(r"[^\w\-]", "_", name or "")
    return os.path.join(_sessions_dir, safe + ".json")


async def _save_session_async(name, urls):
    os.makedirs(_sessions_dir, mode=0o700, exist_ok=True)
    bundle = {"name": name, "saved": int(time.time()), "cookies": [], "storage": {}}
    # cookies for each url
    for u in (urls or []):
        r = await send_to_extension("cookies", {"op": "get", "url": u}, timeout=10)
        if isinstance(r, dict) and "cookies" in r:
            bundle["cookies"].extend(r["cookies"])
    # localStorage snapshot per active tab if no urls
    if urls:
        for u in urls:
            # Open a temp tab for each origin, snapshot, close
            t = await send_to_extension("newTab", {"url": u})
            tid = t.get("tabId") if isinstance(t, dict) else None
            if tid:
                # Wait briefly for page
                await asyncio.sleep(1)
                snap = await send_to_extension("storageOp", {"tabId": tid, "kind": "local", "op": "snapshot"}, timeout=10)
                if isinstance(snap, dict) and snap.get("ok"):
                    bundle["storage"][u] = snap.get("items", {})
                await send_to_extension("closeTab", {"tabId": tid})
    with open(_session_path(name), "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)
    return {"ok": True, "name": name, "path": _session_path(name), "cookies": len(bundle["cookies"]), "origins": len(bundle["storage"])}


async def _load_session_async(name):
    path = _session_path(name)
    if not os.path.isfile(path):
        return {"error": f"Session not found: {name}"}
    with open(path, "r", encoding="utf-8") as f:
        bundle = json.load(f)
    set_count = 0
    for c in bundle.get("cookies", []):
        try:
            url = ("https://" if c.get("secure") else "http://") + str(c.get("domain", "")).lstrip(".") + str(c.get("path", "/"))
            await send_to_extension("cookies", {
                "op": "set", "url": url, "name": c.get("name"), "value": c.get("value"),
                "domain": c.get("domain"), "path": c.get("path"), "secure": c.get("secure"),
            }, timeout=10)
            set_count += 1
        except Exception:
            pass
    storage_restored = 0
    for u, items in (bundle.get("storage") or {}).items():
        t = await send_to_extension("newTab", {"url": u})
        tid = t.get("tabId") if isinstance(t, dict) else None
        if tid:
            await asyncio.sleep(0.8)
            await send_to_extension("storageOp", {"tabId": tid, "kind": "local", "op": "restore", "value": items}, timeout=10)
            await send_to_extension("closeTab", {"tabId": tid})
            storage_restored += 1
    return {"ok": True, "name": name, "cookies_set": set_count, "origins_restored": storage_restored}


def _list_sessions():
    if not os.path.isdir(_sessions_dir):
        return []
    out = []
    for fn in os.listdir(_sessions_dir):
        if fn.endswith(".json"):
            p = os.path.join(_sessions_dir, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    b = json.load(f)
                out.append({
                    "name": b.get("name", fn[:-5]),
                    "path": p,
                    "saved": b.get("saved"),
                    "cookies": len(b.get("cookies", [])),
                    "origins": len(b.get("storage", {})),
                })
            except Exception:
                pass
    return out


# ── Variable substitution in batch ──

def _substitute_vars(cmd, prior_results):
    """Replace ${$N.path.to.value} tokens anywhere in cmd with values from prior_results.

    Example: ${$0.tabId} pulls .tabId from the first prior result.
    """
    if isinstance(cmd, dict):
        return {k: _substitute_vars(v, prior_results) for k, v in cmd.items()}
    if isinstance(cmd, list):
        return [_substitute_vars(x, prior_results) for x in cmd]
    if isinstance(cmd, str):
        def repl(m):
            path = m.group(1)
            try:
                if not path.startswith("$"):
                    return m.group(0)
                # $N.foo.bar
                head, *rest = path[1:].split(".")
                idx = int(head)
                val = prior_results[idx]
                for k in rest:
                    if isinstance(val, dict):
                        val = val.get(k)
                    elif isinstance(val, list) and k.isdigit():
                        val = val[int(k)]
                    else:
                        return m.group(0)
                return val if isinstance(val, str) else json.dumps(val)
            except Exception:
                return m.group(0)
        return _re.sub(r"\$\{(\$[^}]+)\}", repl, cmd)
    return cmd


async def _keep_alive_loop(tab_id, interval):
    """Fire a cheap script eval at a tab on every interval so Zen's unloader
    keeps resetting its idle timer. Any failure is swallowed — the loop keeps
    going until cancelled, since the tab may briefly be navigating or closing
    without us needing to bail."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await send_to_extension("keepAlive", {"tabId": tab_id}, timeout=10)
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


async def _start_keep_alive_async(tab_ids, interval):
    started = []
    for tid in tab_ids:
        existing = _keep_alive_tasks.get(tid)
        if existing and not existing.done():
            existing.cancel()
        task = asyncio.create_task(_keep_alive_loop(tid, interval))
        _keep_alive_tasks[tid] = task
        started.append(tid)
    return started


async def _stop_keep_alive_async(tab_ids):
    stopped = []
    if tab_ids is None:
        tab_ids = list(_keep_alive_tasks.keys())
    for tid in tab_ids:
        task = _keep_alive_tasks.pop(tid, None)
        if task and not task.done():
            task.cancel()
            stopped.append(tid)
    return stopped


def _parse_workflow(filepath, variables=None):
    """Parse a workflow .md file into a list of batch commands."""
    variables = variables or {}
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    blocks = _re.findall(r'```json\s*\n(.*?)\n```', content, _re.DOTALL)

    commands = []
    for block in blocks:
        text = block
        for key, val in variables.items():
            # For placeholders embedded inside a JSON string that itself contains JS code.
            text = text.replace('{{jslit:' + key + '}}', json.dumps(json.dumps(val))[1:-1])
            text = text.replace('{{json:' + key + '}}', json.dumps(val))
            text = text.replace('{{' + key + '}}', str(val))
        try:
            cmd = json.loads(text)
            commands.append(cmd)
        except json.JSONDecodeError as e:
            return None, f"Invalid JSON in workflow block: {e}"

    if not commands:
        return None, "No JSON command blocks found in workflow"
    return commands, None


# ══════════════════════════════════════════════
#  WebSocket Server (Extension connects here)
# ══════════════════════════════════════════════

# websockets rejects any other Origin (or a missing one) with HTTP 403 during
# the handshake, before ws_handler runs: a web page connecting here would
# otherwise replace the real extension and receive its commands.
ALLOWED_WS_ORIGINS = [_re.compile(_re.escape(ALLOWED_WS_ORIGIN_PREFIX) + ".*")]


async def ws_handler(websocket):
    global extension_ws, extension_ws_generation, extension_info
    old_ws = extension_ws
    extension_ws_generation += 1
    my_generation = extension_ws_generation
    extension_ws = websocket

    if old_ws is not None and old_ws is not websocket:
        _log("↻ New extension connection replacing existing one")
        try:
            await old_ws.close(code=1012, reason="replaced by newer ZenLink connection")
        except Exception:
            pass
    _log(f"✅ Browser extension connected (generation {my_generation})")

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                _log(f"⚠️ Bad message from extension: {e}")
                continue
            cmd_id = data.get("id")

            # Heartbeat ping - just ignore
            if data.get("type") == "ping":
                continue
            if data.get("type") == "hello":
                if extension_ws is websocket:
                    extension_info = {"version": data.get("version"), "build": data.get("build")}
                    _log(f"Extension {extension_info['version']} ({extension_info['build']} build)")
                continue

            if cmd_id and cmd_id in pending_commands:
                entry = pending_commands[cmd_id]
                future = entry["future"] if isinstance(entry, dict) else entry
                if not future.done():
                    future.set_result(data.get("result", {}))
            else:
                _log(f"← Extension: {json.dumps(data)[:200]}")
    except websockets.exceptions.ConnectionClosed:
        _log(f"⚠️ Extension disconnected (generation {my_generation})")
    finally:
        if extension_ws is websocket:
            extension_ws = None
            extension_info = {}
        else:
            _log(f"↩ Stale extension connection closed (generation {my_generation}); active connection kept")

        # Fail only commands that were sent over this websocket. A replaced
        # connection must not wipe commands or connection state for the newer
        # websocket.
        for cmd_id, entry in list(pending_commands.items()):
            future = entry["future"] if isinstance(entry, dict) else entry
            ws = entry.get("ws") if isinstance(entry, dict) else websocket
            if ws is websocket:
                if not future.done():
                    action = entry.get("action") if isinstance(entry, dict) else None
                    future.set_result(_disconnected_error("Extension disconnected", action))
                pending_commands.pop(cmd_id, None)


def _log_rejected_ws_origin(connection, request, response):
    if response.status_code == 403:
        _log(f"⛔ Rejected WebSocket connection from origin {request.headers.get('Origin') or '(none)'!r}")


def serve_extension_ws(port):
    # 2.1.0: websockets defaults to a 1 MiB message cap - a full-page screenshot is several MB and the
    # oversized frame closed the socket ("Extension disconnected"). Allow big frames.
    return ws_serve(ws_handler, "127.0.0.1", port, origins=ALLOWED_WS_ORIGINS,
                    process_response=_log_rejected_ws_origin, max_size=256 * 1024 * 1024)


# Action error contract (docs/api-reference.md#action-errors): `error` keeps its text;
# code, retryable, hint and state are additive and machine-readable.
def _action_error(message, code, *, retryable, hint, **state):
    error = {"error": message, "code": code, "retryable": retryable, "hint": hint}
    if state:
        error["state"] = state
    return error


def _safe_to_repeat(action):
    """Whether repeating a command whose outcome is unknown cannot duplicate a side effect."""
    return action is not None and action not in _REPEAT_UNSAFE_ACTIONS


def _disconnected_error(message, action, sent=True):
    return _action_error(
        message, "extension_disconnected",
        retryable=not sent or _safe_to_repeat(action),
        hint=("The extension disconnected after the command was sent, so its effect is unknown; "
              "check the page with browser_snapshot before repeating it." if sent else
              "Check browser_status; the extension reconnects automatically while the browser runs."),
        connected=extension_ws is not None,
    )


async def send_to_extension(action, params=None, timeout=30):
    """Send command to extension and wait for response."""
    result = await _run_in_command_scope(
        action,
        params,
        lambda: _send_to_extension_raw(action, params=params, timeout=timeout),
    )
    _remember_outcome(action, params, result)
    return result


async def _send_to_extension_raw(action, params=None, timeout=30):
    """Send a command over the active extension WebSocket without scheduling."""
    global command_counter

    params = dict(params or {})
    params.setdefault("includeSnapshot", os.environ.get("KITSUNE_POST_ACTION_SNAPSHOT", "1").lower()
                      not in {"0", "false", "no", "off"})
    ws = extension_ws
    if not ws:
        return _disconnected_error(
            "Kitsune extension not connected. Open Firefox or Zen and make sure the Kitsune extension is loaded.",
            action, sent=False)
    
    command_counter += 1
    cmd_id = f"cmd_{command_counter}_{int(time.time())}"
    
    # Spread params FIRST so that the command name is never clobbered by a
    # `params["action"]` field. (Some endpoints — captureNetwork, intercept —
    # legitimately use "action" as a sub-op name; without this ordering, the
    # extension receives the sub-op as the top-level action and reports
    # "Unknown action: start"/"block".)
    command = {
        "id": cmd_id,
        **params,
        "action": action,
    }
    
    future = asyncio.get_running_loop().create_future()
    pending_commands[cmd_id] = {
        "future": future,
        "ws": ws,
        "action": action,
        "tabId": params.get("tabId"),
        "started": time.time(),
    }
    
    try:
        async with _get_ws_send_lock():
            if extension_ws is not ws:
                return _disconnected_error("Extension reconnected before command could be sent", action, sent=False)
            await ws.send(json.dumps(command))
        result = await asyncio.wait_for(future, timeout=timeout)
        if action == "closeTab" and not (isinstance(result, dict) and result.get("error")):
            _cleanup_tab_scheduler_state(params.get("tabId"))
        # The extension cannot tell reads from writes; repeating a read is always safe.
        if (isinstance(result, dict) and result.get("code") and _safe_to_repeat(action)
                and result["code"] not in _PERSISTENT_FAILURE_CODES):
            result["retryable"] = True
        return result
    except asyncio.TimeoutError:
        return _action_error(
            f"Command timed out after {timeout}s", "timeout",
            retryable=_safe_to_repeat(action),
            hint="No reply arrived in time, so the command's effect is unknown; check the page "
                 "with browser_snapshot before repeating it.",
            timeoutSeconds=timeout,
        )
    except websockets.exceptions.ConnectionClosed:
        return _disconnected_error("Extension disconnected while waiting for response", action)
    except Exception as e:
        return {"error": str(e)}
    finally:
        pending_commands.pop(cmd_id, None)


# Action outcome contract (docs/api-reference.md#action-outcomes): a reply without an
# error only proves the command was dispatched. With `expect`, the bridge then checks,
# within a bounded time, that the caller's expected outcome actually happened.
OUTCOME_TIMEOUT_MS = 5000
OUTCOME_POLL_SECONDS = 0.2
_OUTCOME_CONDITIONS = ("url", "url_changed", "selector", "text", "text_gone")
# A check that hit one of these may succeed on the next document, so keep polling.
_OUTCOME_TRANSIENT_CODES = {"navigating", "content_unavailable"}
# Page actions on one existing tab; the outcome is observed in that tab.
_OUTCOME_ACTIONS = {
    "click", "doubleClick", "type", "fill", "setEditableContent", "selectOption", "checkBox",
    "keypress", "submitForm", "formFill", "hover", "scroll", "drag", "focusElement", "blurElement",
    "navigate", "goBack", "goForward", "reloadTabBrowser", "clickAndWaitNavigation",
}


def _parse_expect(action, expect):
    """Validate an `expect` object; return (spec, None) or (None, error message)."""
    if action not in _OUTCOME_ACTIONS:
        return None, f"expect is not supported for action: {action}"
    if not isinstance(expect, dict):
        return None, "expect must be an object"
    if "ref" in expect:
        return None, ("expect.ref is not supported because the post-action snapshot replaces refs; "
                      "use expect.selector")
    unknown = set(expect) - {*_OUTCOME_CONDITIONS, "gone", "value", "timeout"}
    if unknown:
        return None, "Unknown expect keys: " + ", ".join(sorted(unknown))
    kinds = [key for key in _OUTCOME_CONDITIONS if expect.get(key) is not None]
    if len(kinds) != 1:
        return None, "expect needs exactly one of " + ", ".join(_OUTCOME_CONDITIONS)
    kind = kinds[0]
    if kind == "url_changed":
        if expect[kind] is not True:
            return None, "expect.url_changed must be true"
    elif not isinstance(expect[kind], str) or not expect[kind]:
        return None, f"expect.{kind} must be a non-empty string"
    gone, value = expect.get("gone", False), expect.get("value")
    if not isinstance(gone, bool):
        return None, "expect.gone must be a boolean"
    if (gone or value is not None) and kind != "selector":
        return None, "expect.gone and expect.value require expect.selector"
    if gone and value is not None:
        return None, "expect.gone and expect.value are mutually exclusive"
    if value is not None and not isinstance(value, str):
        return None, "expect.value must be a string"
    timeout = expect.get("timeout", OUTCOME_TIMEOUT_MS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or timeout <= 0:
        return None, "expect.timeout must be positive milliseconds"
    spec = {kind: expect[kind], "timeout": timeout}
    if gone:
        spec["gone"] = True
    if value is not None:
        spec["value"] = value
    return spec, None


def _outcome_budget_seconds(spec):
    """Time to allow beyond dispatch: the bounded wait plus the tab reads around it."""
    return spec["timeout"] / 1000 + 45


async def _tab_state(tab_id):
    """Current url/title of a tab, for outcome checks and failure state."""
    tabs = await send_to_extension("getTabs", {}, timeout=10)
    if not isinstance(tabs, dict) or tabs.get("error"):
        return {"tabId": tab_id}
    tab = next((t for t in tabs.get("tabs", []) if t.get("id") == tab_id), None)
    if tab is None:
        return {"tabId": tab_id, "closed": True}
    return {"tabId": tab_id, "url": tab.get("url"), "title": tab.get("title")}


async def _check_outcome(spec, tab_id, before_url, remaining_ms):
    """One observation bounded by remaining_ms; returns (met, observed, final)."""
    wait_s = remaining_ms / 1000 + 5
    waited = True  # a wait that ends unmet has used up the remaining time
    if "url" in spec or "url_changed" in spec:
        # Tab metadata survives document replacement, unlike content-script reads.
        pattern = spec.get("url") or "^(?!%s$)" % _re.escape(before_url or "")
        r = await send_to_extension("waitForUrl", {"tabId": tab_id, "pattern": pattern,
                                                  "timeout": remaining_ms}, timeout=wait_s)
        observed = {"source": "tab", "url": r.get("url")}
        met = r.get("ok") is True
    elif "text" in spec or "text_gone" in spec:
        r = await send_to_extension("waitForText", {"tabId": tab_id, "text": spec.get("text"),
                                                   "text_gone": spec.get("text_gone"),
                                                   "timeout": remaining_ms}, timeout=wait_s)
        met = r.get("ok") is True
        observed = {"source": "page", "textVisible": met if "text" in spec else not met}
    elif "value" in spec:
        waited = False
        r = await send_to_extension("query", {"tabId": tab_id, "selector": spec["selector"],
                                             "fields": ["value"], "limit": 1}, timeout=10)
        item = (r.get("items") or [None])[0]
        if item is None:
            observed, met = {"source": "page", "found": False}, False
        elif item.get("redacted"):
            # Sensitive values are never read back, so this outcome cannot be verified.
            return False, {"source": "page", "found": True, "redacted": True,
                           "hasValue": item.get("hasValue")}, True
        else:
            observed = {"source": "page", "found": True, "value": item.get("value")}
            met = item.get("value") == spec["value"]
    else:
        # A 1 ms waitForElement is a single visibility check; otherwise wait for it to appear.
        waited = not spec.get("gone")
        check_ms = max(1, remaining_ms) if waited else 1
        r = await send_to_extension("waitForElement", {"tabId": tab_id, "selector": spec["selector"],
                                                      "timeout": check_ms, "pollInterval": 100},
                                    timeout=check_ms / 1000 + 5)
        visible = r.get("found") is True
        observed = {"source": "page", "visible": visible}
        met = r.get("found") is False if spec.get("gone") else visible
    # Waits report a timeout as ok:false with an error; anything else failed to observe.
    if r.get("error") and r.get("ok") is not False:
        observed = {"source": observed["source"], "error": r["error"],
                    **({"code": r["code"]} if r.get("code") else {})}
        return False, observed, r.get("code") not in _OUTCOME_TRANSIENT_CODES
    return met, observed, waited


async def _observe_outcome(spec, tab_id, before_url, snapshot):
    """Poll until the expected outcome is observed or spec['timeout'] ms pass."""
    start = time.monotonic()
    # Reuse the post-action snapshot: text already in it needs no further read.
    if "text" in spec and isinstance(snapshot, str) and spec["text"] in snapshot:
        return {"met": True, "observed": {"source": "snapshot", "textVisible": True}, "elapsed": 0}
    deadline = start + spec["timeout"] / 1000
    while True:
        remaining_ms = max(0, math.ceil((deadline - time.monotonic()) * 1000))
        met, observed, final = await _check_outcome(spec, tab_id, before_url, remaining_ms)
        if met or final or time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(OUTCOME_POLL_SECONDS, max(0, deadline - time.monotonic())))
    return {"met": met, "observed": observed, "elapsed": int((time.monotonic() - start) * 1000)}


async def _send_with_expectation(action, params, spec, timeout=30, remember=None):
    result = await _dispatch_with_expectation(action, params, spec, timeout)
    if remember is not None:
        # Ask-before-act keys repeats by the caller's params, which include expect.
        _remember_outcome(action, remember, result)
    return result


async def _dispatch_with_expectation(action, params, spec, timeout):
    """Dispatch an action, then report whether the caller's expected outcome was observed."""
    params = dict(params)
    tab_id = params.get("tabId")
    if tab_id is None:
        # getZoom resolves a missing tabId exactly like the action will (the current
        # window's active tab), so dispatch and observation target the same tab.
        active = await send_to_extension("getZoom", {}, timeout=10)
        if not isinstance(active, dict) or active.get("error"):
            return active
        tab_id = params["tabId"] = active["tabId"]
    before_url = None
    if "url_changed" in spec:
        before_url = (await _tab_state(tab_id)).get("url")
        if before_url is None:
            return {"error": f"Cannot read the URL of tab {tab_id} before dispatch"}
    result = await send_to_extension(action, params, timeout=timeout)
    if not isinstance(result, dict) or result.get("error") or result.get("ok") is False:
        return result
    outcome = await _observe_outcome(spec, tab_id, before_url, result.get("snapshot"))
    expected = dict(spec)
    if outcome["observed"].get("redacted"):
        expected.pop("value")  # never echo a value meant for a sensitive field
    result["dispatched"] = True
    result["outcome"] = {"expected": expected, **outcome}
    if outcome["met"]:
        return result
    state = await _tab_state(tab_id)
    result["outcome"]["state"] = state
    result.update(_action_error(
        f"Expected outcome not observed within {spec['timeout']}ms", "outcome_not_observed",
        retryable=_safe_to_repeat(action),
        hint="The command was dispatched but the expected outcome did not happen; inspect the page "
             "with browser_snapshot before acting again.",
        **state,
    ))
    result["ok"] = False
    return result


# ══════════════════════════════════════════════
#  2.1.0 — File upload (browser_upload_file)
# ══════════════════════════════════════════════
# Pages can't read local files, so the bridge reads them and streams base64
# chunks to the content script, which rebuilds File objects and attaches them.
# Chunk size is a multiple of 3 so every chunk base64-decodes on its own, and
# stays well under the websocket's 1 MiB frame limit.
UPLOAD_CHUNK_BYTES = 384 * 1024
UPLOAD_MAX_BYTES = 1024 * 1024 * 1024


async def _upload_files_async(body):
    import mimetypes
    import uuid
    paths = body.get("paths") or ([body["path"]] if body.get("path") else [])
    if not paths:
        return {"error": "path or paths is required"}
    resolved = []
    for p in paths:
        p = os.path.abspath(os.path.expandvars(os.path.expanduser(str(p))))
        if not os.path.isfile(p):
            return {"error": f"file not found: {p}"}
        if os.path.getsize(p) > UPLOAD_MAX_BYTES:
            return {"error": f"file over 1 GB: {p}"}
        resolved.append(p)
    tab_id = _coerce_tab_id(body.get("tabId"))
    if tab_id is None:
        tabs = await send_to_extension("getTabs", {})
        active = [t for t in (tabs or {}).get("tabs", []) if t.get("active")]
        if not active:
            return {"error": "no active tab - pass tabId"}
        tab_id = active[0]["id"]
    uid = uuid.uuid4().hex[:12]
    files, idx, total, t0 = [], 0, 0, time.time()
    for p in resolved:
        start = idx
        with open(p, "rb") as fh:
            while True:
                raw = fh.read(UPLOAD_CHUNK_BYTES)
                if not raw:
                    break
                b64 = base64.b64encode(raw).decode("ascii")
                r = await send_to_extension("uploadChunk", {"tabId": tab_id, "uploadId": uid, "index": idx, "data": b64}, timeout=30)
                if isinstance(r, dict) and r.get("error"):
                    return {"error": f"chunk {idx} failed: {r['error']}"}
                idx += 1
                total += len(raw)
        files.append({"name": os.path.basename(p), "type": mimetypes.guess_type(p)[0] or "application/octet-stream",
                      "start": start, "end": idx})
    sel, mode = body.get("selector"), body.get("mode") or "auto"
    r = await send_to_extension("uploadCommit", {"tabId": tab_id, "uploadId": uid, "selector": sel,
                                                 "files": files, "mode": mode}, timeout=60)
    if isinstance(r, dict) and not r.get("error"):
        r.update({"tabId": tab_id, "chunks": idx, "bytes": total,
                  "seconds": round(time.time() - t0, 1)})
    return r


JS_FULL_CHUNK = 45000   # content.js caps executeJS results at 50,000 chars


async def _js_full_async(body):
    """2.0.9 - /api/js with {"full": true}: run the code once, park the result in the content-script
    window, then read it back in slices so callers get results longer than the 50 KB cap."""
    tab_id = body.get("tabId")
    wrapped = ("(function(){var __v=eval(%s);window.__zlBig=String(__v===undefined||__v===null?'':__v);"
               "return String(window.__zlBig.length);})()" % json.dumps(body.get("code", "")))
    r = await send_to_extension("executeJS", {"tabId": tab_id, "code": wrapped}, timeout=60)
    if not isinstance(r, dict) or r.get("error") or not str(r.get("result", "")).isdigit():
        return r if isinstance(r, dict) else {"error": "unexpected: %r" % (r,)}
    n, parts = int(r["result"]), []
    for i in range(0, n, JS_FULL_CHUNK):
        rr = await send_to_extension("executeJS", {"tabId": tab_id, "code": "window.__zlBig.slice(%d,%d)" % (i, i + JS_FULL_CHUNK)}, timeout=30)
        if not isinstance(rr, dict) or rr.get("error"):
            return {"error": "chunk at %d failed: %r" % (i, rr)}
        parts.append(rr.get("result", ""))
    await send_to_extension("executeJS", {"tabId": tab_id, "code": "delete window.__zlBig;'ok'"}, timeout=10)
    out = "".join(parts)
    return {"result": out, "length": len(out), "chunks": len(parts), "complete": len(out) == n}


_IMAGE_URL = _re.compile(r"^https?://[^?#]+\.(?:jpe?g|png|gif|webp|avif|bmp)(?:[?#].*)?$", _re.I)


def _wrap_image_url(body):
    """2.0.9 - opening a bare image URL (an ImageDocument) knocked the extension offline. Serve it inside
    a tiny local page instead; the image is #zlimg. Pass {"raw": true} to skip."""
    url = (body or {}).get("url") or ""
    if url and not body.get("raw") and _IMAGE_URL.match(url):
        from urllib.parse import quote
        body = dict(body, url="http://127.0.0.1:%d/view?src=%s" % (HTTP_PORT, quote(url, safe="")), wrappedImage=url)
    return body


# ══════════════════════════════════════════════
#  HTTP API Server (Claude Desktop calls this)
# ══════════════════════════════════════════════


# Action name -> extension action name (for simple forwarding)
BATCH_ACTION_MAP = {
    'navigate':       'navigate',
    'newTab':         'newTab',
    'closeTab':       'closeTab',
    'switchTab':      'switchTab',
    'click':          'click',
    'type':           'type',
    'setEditableContent': 'setEditableContent',
    'fill':           'fill',
    'scroll':         'scroll',
    'hover':          'hover',
    'find':           'find',
    'js':             'executeJS',
    'pageInfo':       'getPageInfo',
    'pageText':       'getPageText',
    'pageTextByTabId':'getPageTextFromTab',
    'tabs':           'getTabs',
    'forms':          'getFormFields',
    'dom':            'getAccessibilityTree',
    'snapshot':       'getSnapshot',
    'highlight':      'highlight',
    'wakeTab':        'wakeTab',
    'reloadExtension':'reloadExtension',
    # 1.4.0 — browser-level
    'pinTab':              'pinTab',
    'muteTab':             'muteTab',
    'duplicateTab':        'duplicateTab',
    'reloadTabBrowser':    'reloadTabBrowser',
    'goBack':              'goBack',
    'goForward':           'goForward',
    'getZoom':             'getZoom',
    'setZoom':             'setZoom',
    'getWindows':          'getWindows',
    'createWindow':        'createWindow',
    'closeWindow':         'closeWindow',
    'focusWindow':         'focusWindow',
    'moveTab':             'moveTab',
    'detachTab':           'detachTab',
    'elementScreenshot':   'elementScreenshot',
    'fullPageScreenshot':  'fullPageScreenshot',
    'cookies':             'cookies',
    'clipboard':           'clipboard',
    'downloads':           'downloads',
    'clearBrowsingData':   'clearBrowsingData',
    'intercept':           'intercept',
    'clickAndWaitNavigation': 'clickAndWaitNavigation',
    # 1.4.0 — content-script-forwarded
    'query':               'query',
    'getHTML':             'getHTML',
    'getLinks':            'getLinks',
    'getImages':           'getImages',
    'getMeta':             'getMeta',
    'getStructuredData':   'getStructuredData',
    'getBounds':           'getBounds',
    'getComputedStyle':    'getComputedStyle',
    'getReadability':      'getReadability',
    'getMarkdown':         'getMarkdown',
    'selectOption':        'selectOption',
    'checkBox':            'checkBox',
    'focusElement':        'focusElement',
    'blurElement':         'blurElement',
    'keypress':            'keypress',
    'doubleClick':         'doubleClick',
    'submitForm':          'submitForm',
    'formFill':            'formFill',
    'drag':                'drag',
    'waitForUrl':          'waitForUrl',
    'waitForTitle':        'waitForTitle',
    'waitForNetworkIdle':  'waitForNetworkIdle',
    'captureNetwork':      'captureNetwork',
    'watchConsole':        'watchConsole',
    'consoleLogs':         'consoleLogs',
    'storageOp':           'storageOp',
    'getIframes':          'getIframes',
    'explainSelector':     'explainSelector',
    'fullPageMetrics':     'fullPageMetrics',
}


async def run_command(cmd):
    """Execute a single batch command asynchronously."""
    action = cmd.get('action', '')
    params = {k: v for k, v in cmd.items() if k != 'action'}
    try:
        # Normalize find params: extension expects "query", users may send "selector"
        if action == 'find' and 'query' not in params and 'selector' in params:
            params['query'] = params.pop('selector')

        effective_action = BATCH_ACTION_MAP.get(action, action)
        policy_err = _check_policy(effective_action, params)
        if policy_err:
            return policy_err
        held = await _ask_before_act(effective_action, params, direct=False)
        if held:
            return held
        _invalidate_cache_for_action(effective_action)

        # Simple forwarding actions (the majority)
        if action in BATCH_ACTION_MAP:
            if 'expect' in params:
                remember = dict(params)
                spec, problem = _parse_expect(effective_action, params.pop('expect'))
                if problem:
                    return {'error': problem}
                return await _send_with_expectation(
                    effective_action, params, spec,
                    timeout=30 + actionability_wait_seconds(effective_action, params), remember=remember)
            if effective_action in ('waitForUrl', 'waitForTitle'):
                timeout_s = params.get('timeout', 10000) / 1000 + 5
                return await send_to_extension(effective_action, params, timeout=timeout_s)
            if effective_action == 'newTab' and params.get('wait'):
                timeout_s = params.get('timeout', 15000) / 1000 + 5
                return await send_to_extension(effective_action, params, timeout=timeout_s)
            wait_s = actionability_wait_seconds(effective_action, params)
            if wait_s:
                return await send_to_extension(effective_action, params, timeout=30 + wait_s)
            return await send_to_extension(effective_action, params)

        # Actions with custom logic
        if action == 'screenshot':
            r = await send_to_extension('screenshot', params)
            if 'dataUrl' in r:
                os.makedirs(SCREENSHOT_DIR, exist_ok=True)
                img = base64.b64decode(r['dataUrl'].split(',')[1])
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                fp = os.path.join(SCREENSHOT_DIR, f'kitsune_{ts}.png')
                with open(fp, 'wb') as f2: f2.write(img)
                return {'saved_to': fp}
            return r
        elif action == 'waitForElement':
            timeout_ms = params.get('timeout', 10000)
            timeout_s = (timeout_ms / 1000) + 5
            return await send_to_extension('waitForElement', params, timeout=timeout_s)
        elif action == 'waitForResult':
            timeout_ms = params.get('timeout', 15000)
            timeout_s = (timeout_ms / 1000) + 5
            return await send_to_extension('waitForResult', params, timeout=timeout_s)
        elif action == 'trustedClick':
            return await trusted_click(params)
        elif action == 'sleep':
            await asyncio.sleep(params.get('ms', 1000) / 1000)
            return {'ok': True}
        elif action == 'parallel':
            sequences = params.get('sequences', [])
            # Validate: navigate/switchTab in parallel sequences must specify tabId
            for i, seq in enumerate(sequences):
                for cmd in seq:
                    a = cmd.get('action', '')
                    if a in ('navigate', 'switchTab') and 'tabId' not in cmd:
                        return {'error': f'Sequence {i}: "{a}" inside parallel requires explicit tabId to avoid race conditions'}
            async def run_sequence(seq):
                results = []
                for c in seq:
                    results.append(await run_command(c))
                    if _is_held(results[-1]):
                        break  # later steps may depend on the held one
                return results
            return list(await asyncio.gather(*[run_sequence(s) for s in sequences]))
        elif action == 'broadcast':
            return await _broadcast_async(params.get('tabIds', []), params.get('command', {}))
        elif action == 'syncBarrier':
            return await _sync_barrier_async(
                params.get('tabIds', []),
                params.get('predicate', '1'),
                timeout=params.get('timeout', 30000),
                poll_interval=params.get('pollInterval', 300),
            )
        elif action == 'retry':
            inner = params.get('command') or {}
            attempts = max(1, int(params.get('maxAttempts', 3)))
            backoff_ms = int(params.get('backoffMs', 500))
            on_errors = params.get('onErrors')
            last = None
            for i in range(attempts):
                last = await run_command(inner)
                if not (isinstance(last, dict) and last.get('error')):
                    return {'ok': True, 'attempts': i + 1, 'result': last}
                # Retrying cannot confirm a held action; only the caller can.
                if _is_held(last):
                    return {'ok': False, 'attempts': i + 1, 'result': last, 'reason': 'confirmation required'}
                # Never blindly repeat a command that may already have taken effect.
                if last.get('retryable') is False:
                    return {'ok': False, 'attempts': i + 1, 'result': last, 'reason': 'not retryable'}
                if on_errors and not any(substr in last.get('error', '') for substr in on_errors):
                    return {'ok': False, 'attempts': i + 1, 'result': last, 'reason': 'error not in onErrors'}
                await asyncio.sleep((backoff_ms / 1000) * (2 ** i))
            return {'ok': False, 'attempts': attempts, 'result': last}
        elif action == 'if':
            cond = params.get('condition')  # JS expression to evaluate on tabId
            tab_id = params.get('tabId')
            then_cmds = params.get('then', [])
            else_cmds = params.get('else', [])
            check = await send_to_extension('executeJS', {'tabId': tab_id, 'code': cond or '1'}, timeout=10)
            truthy = bool(check.get('result')) and check.get('result') not in ('false', '0', 'null', 'undefined', '')
            branch = then_cmds if truthy else else_cmds
            out = []
            for c in branch:
                out.append(await run_command(c))
            return {'branch': 'then' if truthy else 'else', 'condition': check.get('result'), 'results': out}
        elif action == 'while':
            cond = params.get('condition')
            tab_id = params.get('tabId')
            body = params.get('do', [])
            max_iters = int(params.get('maxIterations', 20))
            results = []
            for i in range(max_iters):
                check = await send_to_extension('executeJS', {'tabId': tab_id, 'code': cond or '0'}, timeout=10)
                truthy = bool(check.get('result')) and check.get('result') not in ('false', '0', 'null', 'undefined', '')
                if not truthy:
                    return {'iterations': i, 'results': results, 'stoppedBy': 'condition'}
                step = []
                for c in body:
                    step.append(await run_command(c))
                results.append(step)
            return {'iterations': max_iters, 'results': results, 'stoppedBy': 'maxIterations'}
        elif action == 'try':
            body = params.get('do', [])
            catch = params.get('catch', [])
            try:
                results = []
                for c in body:
                    r = await run_command(c)
                    results.append(r)
                    if isinstance(r, dict) and r.get('error'):
                        raise RuntimeError(r['error'])
                return {'ok': True, 'results': results}
            except Exception as e:
                caught = []
                for c in catch:
                    caught.append(await run_command(c))
                return {'ok': False, 'error': str(e), 'catch_results': caught}
        elif action == 'sequence':
            # Run a list of commands with variable substitution between them
            inner = params.get('do', [])
            results = []
            for command in inner:
                resolved = _substitute_vars(command, results)
                results.append(await run_command(resolved))
                if _is_held(results[-1]):
                    break
            return {'results': results}
        else:
            return {'error': f'Unknown batch action: {action}'}
    except Exception as e:
        return {'error': str(e)}


class _ClientActivity:
    """HTTP requests in flight and when the bridge last had one, for idle shutdown."""

    def __init__(self):
        self._lock = Lock()
        self._in_flight = 0
        self._last = time.monotonic()

    def begin(self):
        with self._lock:
            self._in_flight += 1
            self._last = time.monotonic()

    def end(self):
        with self._lock:
            self._in_flight -= 1
            self._last = time.monotonic()

    def touch(self):
        with self._lock:
            self._last = time.monotonic()

    def in_flight(self):
        with self._lock:
            return self._in_flight

    def idle_seconds(self):
        with self._lock:
            return 0.0 if self._in_flight else time.monotonic() - self._last


_client_activity = _ClientActivity()


class BridgeHTTPServer(ThreadingHTTPServer):
    """Counts each request from accept until its handler thread ends.

    process_request runs in the serve_forever thread, so once shutdown() returns
    every accepted request is already counted and an idle shutdown can drain it.
    """

    def process_request(self, request, client_address):
        _client_activity.begin()
        try:
            super().process_request(request, client_address)
        except BaseException:
            _client_activity.end()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            _client_activity.end()


class BridgeHandler(BaseHTTPRequestHandler):

    def canonical_path(self):
        path = self.path.split("?", 1)[0]
        return _API_ALIASES.get(path, path)

    def authorized(self):
        """Check Host (DNS rebinding) and bearer token. Sends the error itself."""
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            self.send_json(403, {"error": "Host not allowed"})
            return False
        if self.command == "GET" and self.canonical_path() in UNAUTHENTICATED_GET_PATHS:
            return True
        auth = self.headers.get("Authorization", "")
        supplied = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), AUTH_TOKEN.encode()):
            self.send_json(401, {"error": f"Missing or invalid token (see {TOKEN_FILE})"})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        routes = {
            "/": self.handle_status,
            "/api/status": self.handle_status,
            "/api/screenshot": self.handle_screenshot,
            "/api/page-info": self.handle_page_info,
            "/api/page-text": self.handle_page_text,
            "/api/tabs": self.handle_tabs,
            "/api/dom": self.handle_dom,
            "/api/snapshot": self.handle_snapshot,
            "/api/forms": self.handle_forms,
            "/api/version": self.handle_version,
            "/api/workflows": self.handle_list_workflows,
            # 1.4.0 — read-only routes also reachable via GET
            "/api/health": self.handle_health,
            "/api/logs": self.handle_logs,
            "/api/audit": self.handle_audit,
            "/api/scheduler": self.handle_scheduler,
            "/api/get-policy": self.handle_get_policy,
            "/api/list-sessions": self.handle_list_sessions,
            "/api/list-tags": self.handle_list_tags,
            "/view": self.handle_view,
        }
        
        handler = routes.get(self.canonical_path())
        if handler:
            handler()
        else:
            self.send_json(404, {"error": "Not found"})
    
    def do_POST(self):
        if not self.authorized():
            return
        routes = {
            "/api/shutdown": self.handle_shutdown,
            "/api/click": self.handle_click,
            "/api/trusted-click": self.handle_trusted_click,
            "/api/type": self.handle_type,
            "/api/set-editable-content": self.handle_set_editable_content,
            "/api/scroll": self.handle_scroll,
            "/api/hover": self.handle_hover,
            "/api/fill": self.handle_fill,
            "/api/upload-file": self.handle_upload_file,
            "/api/navigate": self.handle_navigate,
            "/api/find": self.handle_find,
            "/api/js": self.handle_js,
            "/api/highlight": self.handle_highlight,
            "/api/clear-highlight": self.handle_clear_highlight,
            "/api/close-tab": self.handle_close_tab,
            "/api/switch-tab": self.handle_switch_tab,
            "/api/new-tab": self.handle_new_tab,
            "/api/wait-for-element": self.handle_wait_for_element,
            "/api/wait-for-text": self.handle_wait_for_text,
            "/api/handle-dialog": lambda: self._forward("handleDialog"),
            "/api/screenshot": self.handle_screenshot,
            "/api/wait-for-result": self.handle_wait_for_result,
            "/api/page-text-by-tab-id": self.handle_page_text_by_tab_id,
            "/api/batch": self.handle_batch,
            "/api/cache": self.handle_cache,
            "/api/workflow": self.handle_workflow,
            # GET endpoints also accepted as POST so callers can supply
            # a tabId in the body to target a non-active tab.
            "/api/page-info": self.handle_page_info,
            "/api/page-text": self.handle_page_text,
            "/api/tabs": self.handle_tabs,
            "/api/forms": self.handle_forms,
            "/api/dom": self.handle_dom,
            "/api/snapshot": self.handle_snapshot,
            # Parallel multi-tab work
            "/api/wake-tab": self.handle_wake_tab,
            "/api/keep-alive": self.handle_keep_alive,
            "/api/keep-alive-stop": self.handle_keep_alive_stop,
            "/api/reload-extension": self.handle_reload_extension,
            # 1.4.0 — generic passthrough handlers (forward body to extension action)
            "/api/query": lambda: self._forward("query"),
            "/api/html": lambda: self._forward("getHTML"),
            "/api/links": lambda: self._forward("getLinks"),
            "/api/images": lambda: self._forward("getImages"),
            "/api/meta": lambda: self._forward("getMeta"),
            "/api/structured-data": lambda: self._forward("getStructuredData"),
            "/api/bounds": lambda: self._forward("getBounds"),
            "/api/computed-style": lambda: self._forward("getComputedStyle"),
            "/api/readability": lambda: self._forward("getReadability"),
            "/api/markdown": lambda: self._forward("getMarkdown"),
            "/api/select-option": lambda: self._forward("selectOption"),
            "/api/check": lambda: self._forward("checkBox"),
            "/api/focus": lambda: self._forward("focusElement"),
            "/api/blur": lambda: self._forward("blurElement"),
            "/api/keypress": lambda: self._forward("keypress"),
            "/api/double-click": lambda: self._forward("doubleClick"),
            "/api/submit-form": lambda: self._forward("submitForm"),
            "/api/form-fill": lambda: self._forward("formFill"),
            "/api/drag": lambda: self._forward("drag"),
            "/api/wait-for-url": lambda: self._forward("waitForUrl", default_timeout=20),
            "/api/wait-for-title": lambda: self._forward("waitForTitle", default_timeout=20),
            "/api/wait-for-network-idle": lambda: self._forward("waitForNetworkIdle", default_timeout=30),
            "/api/capture-network": lambda: self._forward("captureNetwork"),
            "/api/watch-console": lambda: self._forward("watchConsole"),
            "/api/console-logs": lambda: self._forward("consoleLogs"),
            "/api/storage": lambda: self._forward("storageOp"),
            "/api/iframes": lambda: self._forward("getIframes"),
            "/api/explain-selector": lambda: self._forward("explainSelector"),
            "/api/full-page-metrics": lambda: self._forward("fullPageMetrics"),
            "/api/pin-tab": lambda: self._forward("pinTab"),
            "/api/mute-tab": lambda: self._forward("muteTab"),
            "/api/duplicate-tab": lambda: self._forward("duplicateTab"),
            "/api/reload-tab": lambda: self._forward("reloadTabBrowser"),
            "/api/back": lambda: self._forward("goBack"),
            "/api/forward": lambda: self._forward("goForward"),
            "/api/get-zoom": lambda: self._forward("getZoom"),
            "/api/set-zoom": lambda: self._forward("setZoom"),
            "/api/windows": lambda: self._forward("getWindows"),
            "/api/create-window": lambda: self._forward("createWindow"),
            "/api/close-window": lambda: self._forward("closeWindow"),
            "/api/focus-window": lambda: self._forward("focusWindow"),
            "/api/move-tab": lambda: self._forward("moveTab"),
            "/api/detach-tab": lambda: self._forward("detachTab"),
            "/api/element-screenshot": lambda: self._forward("elementScreenshot", default_timeout=20),
            "/api/full-page-screenshot": lambda: self._forward("fullPageScreenshot", default_timeout=60),
            "/api/cookies": lambda: self._forward("cookies"),
            "/api/clipboard": lambda: self._forward("clipboard"),
            "/api/downloads": lambda: self._forward("downloads"),
            "/api/clear-browsing-data": lambda: self._forward("clearBrowsingData", default_timeout=60),
            "/api/intercept": lambda: self._forward("intercept"),
            "/api/click-and-wait-navigation": lambda: self._forward("clickAndWaitNavigation", default_timeout=20),
            # 1.4.0 — bridge-only orchestration
            "/api/broadcast": self.handle_broadcast,
            "/api/sync-barrier": self.handle_sync_barrier,
            "/api/retry": self.handle_retry,
            "/api/tag-tab": self.handle_tag_tab,
            "/api/resolve-tag": self.handle_resolve_tag,
            "/api/list-tags": self.handle_list_tags,
            "/api/untag-tab": self.handle_untag_tab,
            "/api/tab-pool": self.handle_tab_pool,
            "/api/pool-acquire": self.handle_pool_acquire,
            "/api/pool-release": self.handle_pool_release,
            "/api/save-session": self.handle_save_session,
            "/api/load-session": self.handle_load_session,
            "/api/list-sessions": self.handle_list_sessions,
            "/api/delete-session": self.handle_delete_session,
            "/api/health": self.handle_health,
            "/api/logs": self.handle_logs,
            "/api/audit": self.handle_audit,
            "/api/scheduler": self.handle_scheduler,
            "/api/set-policy": self.handle_set_policy,
            "/api/get-policy": self.handle_get_policy,
        }

        handler = routes.get(self.canonical_path())
        if handler:
            handler()
        else:
            self.send_json(404, {"error": "Not found"})
    
    # ── GET Handlers ──
    
    def handle_status(self):
        self.send_json(200, {
            "status": "running",
            "extension_connected": extension_ws is not None,
            "extension_version": extension_info.get("version"),
            "extension_build": extension_info.get("build"),
            "port": HTTP_PORT,
            "ws_port": WS_PORT,
            "screenshot_dir": SCREENSHOT_DIR,
            "bridge_version": BRIDGE_VERSION,
            "build": BRIDGE_BUILD,
            "extension_generation": extension_ws_generation,
            "scheduler": _scheduler_snapshot(),
            "keep_alive_tabs": [tid for tid, t in _keep_alive_tasks.items() if not t.done()],
        })
    
    def handle_shutdown(self):
        assert loop is not None and shutdown_event is not None
        self.send_json(200, {"status": "stopping"})
        loop.call_soon_threadsafe(shutdown_event.set)

    def handle_screenshot(self):
        body = self.read_body() if self.command == "POST" else {}
        result = self.run_async(send_to_extension("screenshot", body))
        if "error" in result:
            self.send_json(500, result)
            return
        
        # Save screenshot to file
        if result.get("dataUrl") and self.command == "GET":
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            filename = f"kitsune_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            filepath = os.path.join(SCREENSHOT_DIR, filename)
            
            # Strip data URL prefix
            b64 = result["dataUrl"].split(",", 1)[1] if "," in result["dataUrl"] else result["dataUrl"]
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(b64))
            
            result["saved_to"] = filepath
            result["filename"] = filename
            # Don't send the full base64 back over HTTP (too large)
            del result["dataUrl"]
        
        self.send_json(200, result)
    
    def handle_page_info(self):
        params = self._tab_params()
        # Dialogs may occur independently of bridge commands; never cache this history.
        result = self.run_async(send_to_extension("getPageInfo", params))
        self.send_json(200, result)

    def handle_page_text(self):
        params = self._tab_params()
        key = _cache_key("getPageText", params)
        cached = _cache_get(key)
        if cached:
            self.send_json(200, {**cached, "_cached": True})
            return
        result = self.run_async(send_to_extension("getPageText", params))
        if "error" not in result:
            _cache_set(key, result)
        self.send_json(200, result)

    def handle_tabs(self):
        # Tabs change through async navigation and outside the bridge; never cache the inventory.
        result = self.run_async(send_to_extension("getTabs"))
        self.send_json(200, result)

    def handle_dom(self):
        params = {"depth": 6, **self._tab_params()}
        result = self.run_async(send_to_extension("getAccessibilityTree", params))
        self.send_json(200, result)

    def handle_snapshot(self):
        body = self.read_body() if self.command == 'POST' else {}
        params = {key: body[key] for key in ('tabId', 'selector', 'ref', 'maxDepth')
                  if body.get(key) is not None}
        max_depth = params.get('maxDepth')
        if max_depth is not None and (type(max_depth) is not int or max_depth < 0):
            self.send_json(200, {"error": "max_depth must be a non-negative integer"})
            return
        result = self.run_async(send_to_extension("getSnapshot", params))
        self.send_json(200, result)

    def handle_forms(self):
        result = self.run_async(send_to_extension("getFormFields", self._tab_params()))
        self.send_json(200, result)

    def handle_version(self):
        info = {
            "bridge": BRIDGE_VERSION,
            "api": API_VERSION,
            "extension_connected": extension_ws is not None,
            "extension_version": extension_info.get("version"),
            "extension_build": extension_info.get("build"),
        }
        if extension_ws:
            try:
                result = self.run_async(send_to_extension("ping"), timeout=5)
                info["extension"] = result.get("version", "unknown")
            except Exception:
                info["extension"] = "unreachable"
        else:
            info["extension"] = "not connected"
        self.send_json(200, info)

    def handle_list_workflows(self):
        available = []
        if os.path.isdir(WORKFLOW_DIR):
            available = [f[:-3] for f in os.listdir(WORKFLOW_DIR) if f.endswith('.md')]
        self.send_json(200, {"workflows": available, "directory": WORKFLOW_DIR})

    # ── POST Handlers ──

    def _held(self, action, body):
        """Apply ask-before-act to a direct call; send the held error and return True if held."""
        if not _policy["ask"]:
            body.pop("confirmToken", None)
            return False
        held = self.run_async(_ask_before_act(action, body, direct=True))
        if held:
            self.send_json(403, held)
        return bool(held)

    def _send_extension_action(self, action, body, timeout=35):
        policy_err = _check_policy(action, body)
        if policy_err:
            self.send_json(403, policy_err)
            return
        spec = None
        if 'expect' in body:
            spec, problem = _parse_expect(action, body['expect'])
            if problem:
                self.send_json(200, {"error": problem})
                return
        if self._held(action, body):
            return
        _invalidate_cache_for_action(action)
        t0 = time.time()
        try:
            if action in ('waitForUrl', 'waitForTitle'):
                timeout = body.get('timeout', 10000) / 1000 + 5
            elif action == 'newTab' and body.get('wait'):
                timeout = body.get('timeout', 15000) / 1000 + 5
            else:
                timeout += actionability_wait_seconds(action, body)
            if spec is None:
                result = self.run_async(send_to_extension(action, body, timeout=timeout), timeout=timeout + 5)
            else:
                params = {k: v for k, v in body.items() if k != 'expect'}
                result = self.run_async(_send_with_expectation(action, params, spec, timeout=timeout,
                                                               remember=dict(body)),
                                        timeout=timeout + _outcome_budget_seconds(spec))
        except Exception as e:
            result = {"error": str(e)}
        _audit(
            action,
            body,
            isinstance(result, dict) and not result.get("error"),
            (result or {}).get("error") if isinstance(result, dict) else None,
            (time.time() - t0) * 1000,
        )
        self.send_json(200, result)
    
    def handle_upload_file(self):
        body = self.read_body()
        policy_err = _check_policy("uploadFile", body)
        if policy_err:
            self.send_json(403, policy_err)
            return
        if self._held("uploadFile", body):
            return
        _invalidate_cache_for_action("uploadFile")
        t0 = time.time()
        try:
            result = self.run_async(_upload_files_async(body), timeout=float(body.get("timeout", 300)))
        except Exception as e:
            result = {"error": str(e)}
        ok = isinstance(result, dict) and not result.get("error")
        _audit("uploadFile", body, ok, None if ok else (result or {}).get("error"), (time.time() - t0) * 1000)
        self.send_json(200, result)

    def handle_click(self):
        self._send_extension_action("click", self.read_body())

    def handle_trusted_click(self):
        body = self.read_body()
        policy_err = _check_policy("trustedClick", body)
        if policy_err:
            self.send_json(403, policy_err)
            return
        if self._held("trustedClick", body):
            return
        _invalidate_cache_for_action("trustedClick")
        t0 = time.time()
        result = self.run_async(trusted_click(body))
        _audit("trustedClick", body, isinstance(result, dict) and not result.get("error"), (result or {}).get("error") if isinstance(result, dict) else None, (time.time() - t0) * 1000)
        self.send_json(200, result)
    
    def handle_type(self):
        self._send_extension_action("type", self.read_body())

    def handle_set_editable_content(self):
        self._send_extension_action("setEditableContent", self.read_body())
    
    def handle_scroll(self):
        self._send_extension_action("scroll", self.read_body())
    
    def handle_hover(self):
        self._send_extension_action("hover", self.read_body())
    
    def handle_fill(self):
        self._send_extension_action("fill", self.read_body())
    
    def handle_navigate(self):
        self._send_extension_action("navigate", _wrap_image_url(self.read_body()))

    def handle_view(self):
        """2.0.9 - local wrapper page for bare image URLs (see _wrap_image_url)."""
        import html as _html
        from urllib.parse import urlparse, parse_qs
        src = parse_qs(urlparse(self.path).query).get("src", [""])[0]
        page = ("<!doctype html><html><head><meta charset='utf-8'><title>ZenLink view</title></head>"
                "<body style='margin:0;background:#111'><img id='zlimg' src=\"" + _html.escape(src, quote=True) +
                "\" style='display:block;max-width:100%'></body></html>")
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    
    def handle_find(self):
        body = self.read_body()
        # Extension expects "query", users may send "selector"
        if 'query' not in body and 'selector' in body:
            body['query'] = body.pop('selector')
        result = self.run_async(send_to_extension("find", body))
        self.send_json(200, result)
    
    def handle_js(self):
        body = self.read_body()
        if body.get("full"):
            if self._held("executeJS", body):
                return
            try:
                result = self.run_async(_js_full_async(body), timeout=float(body.get("timeout", 180)))
            except Exception as e:
                result = {"error": str(e)}
            self.send_json(200, result)
            return
        self._send_extension_action("executeJS", body)
    
    def handle_highlight(self):
        self._send_extension_action("highlight", self.read_body())

    def handle_clear_highlight(self):
        self._send_extension_action("clearHighlight", {})

    def handle_close_tab(self):
        body = self.read_body()
        if body.get("force"):
            try:
                self.run_async(send_to_extension("neuterUnload", {"tabId": body.get("tabId")}, timeout=8), timeout=10)
            except Exception:
                pass   # tab may be hung or already gone - still try to close it
        self._send_extension_action("closeTab", body)

    def handle_switch_tab(self):
        self._send_extension_action("switchTab", self.read_body())

    def handle_new_tab(self):
        self._send_extension_action("newTab", _wrap_image_url(self.read_body()))

    def handle_wait_for_element(self):
        body = self.read_body()
        # waitForElement can take up to timeout + buffer; extend the WS timeout accordingly
        timeout_ms = body.get("timeout", 10000)
        timeout_s = (timeout_ms / 1000) + 5
        result = self.run_async(send_to_extension("waitForElement", body, timeout=timeout_s))
        self.send_json(200, result)

    def handle_wait_for_text(self):
        body = self.read_body()
        timeout_ms = body.get("timeout", 10000)
        if not isinstance(timeout_ms, (int, float)) or timeout_ms < 0:
            self.send_json(400, {"error": "timeout must be non-negative milliseconds"})
            return
        timeout_s = timeout_ms / 1000 + 5
        result = self.run_async(
            send_to_extension("waitForText", body, timeout=timeout_s), timeout=timeout_s + 1)
        self.send_json(200, result)

    def handle_wait_for_result(self):
        body = self.read_body()
        if self._held("waitForResult", body):
            return
        timeout_ms = body.get("timeout", 15000)
        timeout_s = (timeout_ms / 1000) + 5
        result = self.run_async(send_to_extension("waitForResult", body, timeout=timeout_s))
        self.send_json(200, result)

    def handle_page_text_by_tab_id(self):
        body = self.read_body()
        result = self.run_async(send_to_extension("getPageTextFromTab", body))
        self.send_json(200, result)

    def handle_wake_tab(self):
        self._send_extension_action("wakeTab", self.read_body(), timeout=20)

    def handle_keep_alive(self):
        body = self.read_body()
        tab_ids = body.get("tabIds", []) or []
        try:
            interval = max(10, int(body.get("intervalSeconds", 60)))
        except (TypeError, ValueError):
            interval = 60
        started = self.run_async(_start_keep_alive_async(tab_ids, interval))
        self.send_json(200, {
            "ok": True,
            "keeping_alive": started,
            "interval_seconds": interval,
            "active_total": len([t for t in _keep_alive_tasks.values() if not t.done()]),
        })

    def handle_keep_alive_stop(self):
        body = self.read_body()
        tab_ids = body.get("tabIds")  # None means stop all
        stopped = self.run_async(_stop_keep_alive_async(tab_ids))
        self.send_json(200, {
            "ok": True,
            "stopped": stopped,
            "active_total": len([t for t in _keep_alive_tasks.values() if not t.done()]),
        })

    def handle_reload_extension(self):
        # Extension acks then reloads itself ~100ms later. Use a short timeout
        # — if no ack arrives, the extension is unreachable and reloading is
        # moot anyway.
        self._send_extension_action("reloadExtension", {}, timeout=5)

    # ── 1.4.0 generic forwarder ──

    def _forward(self, action, default_timeout=15):
        """Forward body + policy check + audit to a named extension action."""
        self._send_extension_action(action, self.read_body(), timeout=default_timeout)

    # ── 1.4.0 orchestration ──

    def handle_broadcast(self):
        body = self.read_body()
        result = self.run_async(_broadcast_async(body.get("tabIds", []), body.get("command", {}), timeout=body.get("timeout", 30)))
        self.send_json(200, result)

    def handle_sync_barrier(self):
        body = self.read_body()
        if self._held("syncBarrier", body):
            return
        result = self.run_async(_sync_barrier_async(
            body.get("tabIds", []),
            body.get("predicate", "1"),
            timeout=body.get("timeout", 30000),
            poll_interval=body.get("pollInterval", 300),
        ))
        self.send_json(200, result)

    def handle_retry(self):
        body = self.read_body()
        cmd = {
            "action": "retry",
            **body,
        }
        result = self.run_async(run_command(cmd))
        self.send_json(200, result)

    # ── Tag tabs ──

    def handle_tag_tab(self):
        body = self.read_body()
        name = body.get("name")
        tab_id = body.get("tabId")
        if not name or not tab_id:
            self.send_json(400, {"error": "name and tabId required"})
            return
        _tags[name] = tab_id
        self.send_json(200, {"ok": True, "name": name, "tabId": tab_id, "totalTags": len(_tags)})

    def handle_resolve_tag(self):
        body = self.read_body()
        name = body.get("name")
        tab_id = _tags.get(name)
        if tab_id is None:
            self.send_json(404, {"error": f"Tag not found: {name}"})
            return
        self.send_json(200, {"name": name, "tabId": tab_id})

    def handle_list_tags(self):
        self.send_json(200, {"tags": dict(_tags), "count": len(_tags)})

    def handle_untag_tab(self):
        body = self.read_body()
        name = body.get("name")
        existed = name in _tags
        _tags.pop(name, None)
        self.send_json(200, {"ok": True, "removed": existed})

    # ── Tab pool ──

    def handle_tab_pool(self):
        body = self.read_body()
        size = int(body.get("size", 0))
        url = body.get("url", "about:blank")
        result = self.run_async(_ensure_tab_pool(size, url))
        self.send_json(200, result)

    def handle_pool_acquire(self):
        if not _tab_pool["available"]:
            # Auto-grow if empty
            if _tab_pool["size"] == 0:
                self.send_json(400, {"error": "Tab pool not initialized; call /api/tab-pool first"})
                return
            grow = self.run_async(_ensure_tab_pool(_tab_pool["size"] + 1, _tab_pool["url"]))
            if "error" in grow:
                self.send_json(500, grow)
                return
        tab_id = _tab_pool["available"].pop(0)
        _tab_pool["in_use"].add(tab_id)
        self.send_json(200, {"ok": True, "tabId": tab_id, **_pool_status()})

    def handle_pool_release(self):
        body = self.read_body()
        tab_id = body.get("tabId")
        if tab_id is None:
            self.send_json(400, {"error": "tabId required"})
            return
        _tab_pool["in_use"].discard(tab_id)
        if tab_id not in _tab_pool["available"]:
            _tab_pool["available"].append(tab_id)
        self.send_json(200, {"ok": True, **_pool_status()})

    # ── Sessions ──

    def handle_save_session(self):
        body = self.read_body()
        if self._held("saveSession", body):
            return
        name = body.get("name") or "default"
        urls = body.get("urls") or []
        result = self.run_async(_save_session_async(name, urls))
        self.send_json(200, result)

    def handle_load_session(self):
        body = self.read_body()
        if self._held("loadSession", body):
            return
        name = body.get("name") or "default"
        result = self.run_async(_load_session_async(name))
        self.send_json(200, result)

    def handle_list_sessions(self):
        self.send_json(200, {"sessions": _list_sessions(), "dir": _sessions_dir})

    def handle_delete_session(self):
        body = self.read_body()
        name = body.get("name")
        path = _session_path(name)
        if os.path.isfile(path):
            os.remove(path)
            self.send_json(200, {"ok": True, "removed": name})
        else:
            self.send_json(404, {"error": f"Session not found: {name}"})

    # ── Observability ──

    def handle_health(self):
        active_keep_alive = [tid for tid, t in _keep_alive_tasks.items() if not t.done()]
        self.send_json(200, {
            "bridge_version": BRIDGE_VERSION,
            "api_version": API_VERSION,
            "extension_connected": extension_ws is not None,
            "extension_version": extension_info.get("version"),
            "extension_build": extension_info.get("build"),
            "extension_generation": extension_ws_generation,
            "ports": {"http": HTTP_PORT, "ws": WS_PORT},
            "keep_alive_tabs": active_keep_alive,
            "scheduler": _scheduler_snapshot(),
            "tab_pool": _pool_status(),
            "tags": dict(_tags),
            "policy": dict(_policy),
            "sessions_dir": _sessions_dir,
            "screenshot_dir": SCREENSHOT_DIR,
            "audit_entries": len(_audit_log),
            "log_entries": len(_log_buffer),
        })

    def handle_logs(self):
        body = self.read_body() if self.command == "POST" else {}
        limit = int(body.get("limit", 200))
        since_ts = body.get("since")
        lines = _log_buffer[-limit:]
        if since_ts:
            lines = [l for l in lines if l >= since_ts]  # works on timestamp prefix
        self.send_json(200, {"count": len(lines), "lines": lines})

    def handle_audit(self):
        body = self.read_body() if self.command == "POST" else {}
        limit = int(body.get("limit", 100))
        since = int(body.get("since", 0))
        items = [a for a in _audit_log if a["ts"] >= since][-limit:]
        self.send_json(200, {"count": len(items), "entries": items, "total": len(_audit_log)})

    def handle_scheduler(self):
        self.send_json(200, _scheduler_snapshot())

    def handle_set_policy(self):
        body = self.read_body()
        invalid = _invalid_ask_policy(body)
        if invalid:
            self.send_json(400, {"error": f"Invalid policy: {invalid}", "code": "invalid_policy",
                                 "policy": dict(_policy)})
            return
        if _policy["askLocked"]:
            # Started with --ask-before-act: the policy can only be tightened.
            loosened = []
            if "ask" in body and not body["ask"]:
                loosened.append("ask cannot be turned off")
            if "askUrls" in body and not set(_policy["askUrls"]) <= set(body["askUrls"] or []):
                loosened.append("askUrls cannot drop patterns")
            if loosened:
                self.send_json(403, {"error": "Ask-before-act is locked for this bridge (started with "
                                              f"{ASK_BEFORE_ACT_ENV}=1 or --ask-before-act): " + "; ".join(loosened),
                                     "code": "policy_locked", "policy": dict(_policy)})
                return
        if "ask" in body: _policy["ask"] = body["ask"]
        if "askUrls" in body: _policy["askUrls"] = list(body["askUrls"])
        if "allow" in body: _policy["allow"] = list(body["allow"])
        if "deny" in body: _policy["deny"] = list(body["deny"])
        if "readonly" in body: _policy["readonly"] = bool(body["readonly"])
        self.send_json(200, {"ok": True, "policy": dict(_policy)})

    def handle_get_policy(self):
        self.send_json(200, dict(_policy))

    def handle_batch(self):
        body = self.read_body()
        commands = body.get('commands', [])
        stop_on_warning = body.get('stopOnWarning', False)

        async def run_all():
            results = []
            for cmd in commands:
                r = await run_command(cmd)
                results.append(r)
                # Later commands may depend on a held one, so never run past it.
                if _is_held(r):
                    r['_stopped'] = True
                    break
                # Stop early if a command returned a warning/redirect and stopOnWarning is set
                if stop_on_warning and isinstance(r, dict) and (r.get('warning') or r.get('error')):
                    r['_stopped'] = True
                    break
            return results

        # Commands run in sequence, so the deadline must cover every wait in the batch.
        results = self.run_async(run_all(), timeout=max(300, batch_wait_seconds(commands) + 15))
        self.send_json(200, {'results': results})

    def handle_cache(self):
        global CACHE_TTL
        body = self.read_body()
        action = body.get("action", "clear")
        if action == "clear":
            _cache.clear()
            self.send_json(200, {"ok": True, "cleared": True})
        elif action == "status":
            self.send_json(200, {"entries": len(_cache), "ttl": CACHE_TTL})
        elif action == "ttl":
            CACHE_TTL = body.get("seconds", 5)
            self.send_json(200, {"ok": True, "ttl": CACHE_TTL})
        else:
            self.send_json(400, {"error": f"Unknown cache action: {action}"})

    def handle_workflow(self):
        body = self.read_body()
        name = body.get("name", "")
        variables = body.get("variables", {})

        if not name:
            self.send_json(400, {"error": "Missing 'name' parameter"})
            return

        # Sanitize filename
        safe_name = _re.sub(r'[^\w\-]', '', name)
        filepath = os.path.join(WORKFLOW_DIR, f"{safe_name}.md")

        if not os.path.isfile(filepath):
            available = []
            if os.path.isdir(WORKFLOW_DIR):
                available = [f[:-3] for f in os.listdir(WORKFLOW_DIR) if f.endswith('.md')]
            self.send_json(404, {"error": f"Workflow '{name}' not found", "available": available})
            return

        commands, err = _parse_workflow(filepath, variables)
        if err:
            self.send_json(400, {"error": err})
            return

        async def run_workflow():
            results = []
            for cmd in commands:
                r = await run_command(cmd)
                results.append(r)
                if isinstance(r, dict) and r.get('error'):
                    break
            return results

        results = self.run_async(run_workflow(), timeout=300)
        self.send_json(200, {"workflow": name, "steps": len(commands), "results": results})

    # ── Helpers ──
    
    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[{now()}] ⚠️ Bad request body: {e}")
            return {}

    def _tab_params(self):
        """Read tabId from POST body if present. Returns {} for GET or absent tabId.

        Lets the same handler serve both GET (active tab) and POST (with
        {"tabId": N} to target a specific tab) without duplicating logic.
        """
        if self.command != 'POST':
            return {}
        body = self.read_body()
        tab_id = body.get('tabId')
        return {'tabId': tab_id} if tab_id is not None else {}
    
    def send_json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def do_OPTIONS(self):
        # No CORS: only local non-browser clients (the MCP server) may call the API.
        self.send_response(405)
        self.end_headers()
    
    def run_async(self, coro, timeout=35):
        """Run async coroutine from sync HTTP handler."""
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)
    
    def log_message(self, format, *args):
        print(f"[{now()}] HTTP {args[0]}" if args else "")


def now():
    return datetime.now().strftime("%H:%M:%S")


# ══════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════

loop = None
shutdown_event = None
_idle_shutdown_lock = None  # lifecycle lock an idle shutdown holds until the pid lock is released


def _idle_timeout_seconds(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid seconds: {value!r}") from None
    if not 0 <= seconds < float("inf"):
        raise argparse.ArgumentTypeError(f"must be a finite number >= 0, got {value!r}")
    return seconds


def _bridge_busy():
    """Client-started work that has no HTTP request in flight but must not be cut off."""
    return bool(pending_commands) or any(not t.done() for t in _keep_alive_tasks.values())


async def _idle_shutdown_watch(idle_timeout):
    global _idle_shutdown_lock
    while True:
        await asyncio.sleep(min(IDLE_POLL_SECONDS, idle_timeout))
        if _bridge_busy():
            _client_activity.touch()
            continue
        if _client_activity.idle_seconds() < idle_timeout:
            continue
        # Holding lifecycle_lock until exit makes a client that restarts the
        # bridge on demand wait for this daemon instead of racing its pid lock.
        _idle_shutdown_lock = bridge_control.try_lifecycle_lock()
        if _idle_shutdown_lock is None:
            continue  # A client is starting or stopping the bridge right now.
        print(f"[{now()}] 💤 No client activity for {idle_timeout:g}s; shutting down")
        shutdown_event.set()
        return


async def main(idle_timeout=0):
    global loop, shutdown_event
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)
    
    ws_server = await serve_extension_ws(WS_PORT)
    print(f"[{now()}] 🔌 WebSocket server on ws://localhost:{WS_PORT}")
    
    http_server = None
    idle_watch = None
    try:
        # Start HTTP server in thread
        http_server = BridgeHTTPServer(("127.0.0.1", HTTP_PORT), BridgeHandler)
        http_thread = Thread(target=http_server.serve_forever, daemon=True)
        http_thread.start()
        print(f"[{now()}] 🌐 HTTP API server on http://localhost:{HTTP_PORT}")
    
        print(f"[{now()}] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"[{now()}] Kitsune bridge is ready!")
        print(f"[{now()}] Waiting for extension to connect...")
        print(f"[{now()}] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print()
        print("API Endpoints:")
        print(f"  GET  http://localhost:{HTTP_PORT}/api/status             - Connection status")
        print(f"  GET  http://localhost:{HTTP_PORT}/api/screenshot          - Capture page")
        print(f"  GET  http://localhost:{HTTP_PORT}/api/page-info           - Page metadata")
        print(f"  GET  http://localhost:{HTTP_PORT}/api/page-text           - Page text content")
        print(f"  GET  http://localhost:{HTTP_PORT}/api/tabs                - List open tabs")
        print(f"  GET  http://localhost:{HTTP_PORT}/api/dom                 - Accessibility tree")
        print(f"  GET  http://localhost:{HTTP_PORT}/api/forms               - Form fields")
        print(f"  POST http://localhost:{HTTP_PORT}/api/click               - Click element")
        print(f"  POST http://localhost:{HTTP_PORT}/api/type                - Type text")
        print(f"  POST http://localhost:{HTTP_PORT}/api/scroll              - Scroll page")
        print(f"  POST http://localhost:{HTTP_PORT}/api/hover               - Hover element")
        print(f"  POST http://localhost:{HTTP_PORT}/api/fill                - Fill form field")
        print(f"  POST http://localhost:{HTTP_PORT}/api/navigate            - Go to URL")
        print(f"  POST http://localhost:{HTTP_PORT}/api/find                - Find elements")
        print(f"  POST http://localhost:{HTTP_PORT}/api/js                  - Execute JavaScript")
        print(f"  POST http://localhost:{HTTP_PORT}/api/highlight           - Highlight element")
        print(f"  POST http://localhost:{HTTP_PORT}/api/wait-for-element    - Wait for element to appear")
        print(f"  POST http://localhost:{HTTP_PORT}/api/wait-for-result     - Poll JS expression until non-empty")
        print(f"  GET  http://localhost:{HTTP_PORT}/api/version             - Bridge/extension version info")
        print(f"  GET  http://localhost:{HTTP_PORT}/api/workflows           - List available workflows")
        print(f"  POST http://localhost:{HTTP_PORT}/api/workflow            - Execute a named workflow")
        print(f"  POST http://localhost:{HTTP_PORT}/api/cache               - Cache control (clear/status/ttl)")
        print(f"  POST http://localhost:{HTTP_PORT}/api/wake-tab            - Reload a discarded tab and wait for ready")
        print(f"  POST http://localhost:{HTTP_PORT}/api/keep-alive          - Pin a list of tabs against the Tab Unloader")
        print(f"  POST http://localhost:{HTTP_PORT}/api/keep-alive-stop     - Stop keep-alive (omit tabIds to stop all)")
        print()

        if idle_timeout:
            _client_activity.touch()
            idle_watch = asyncio.create_task(_idle_shutdown_watch(idle_timeout))
            print(f"[{now()}] Idle shutdown after {idle_timeout:g}s without client activity")
    
        await shutdown_event.wait()

    finally:
        if idle_watch is not None:
            idle_watch.cancel()
        if http_server is not None:
            await asyncio.to_thread(http_server.shutdown)
            http_server.server_close()
            if _idle_shutdown_lock is not None:
                # Requests accepted before the listener closed still run on this loop.
                while _client_activity.in_flight():
                    await asyncio.sleep(0.05)
        ws_server.close()
        await ws_server.wait_closed()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def run(argv=None):
    parser = argparse.ArgumentParser(description="Kitsune shared browser bridge")
    parser.add_argument("command", nargs="?", choices=("start", "stop", "status"), default="start")
    parser.add_argument(
        "--idle-timeout", type=_idle_timeout_seconds, metavar="SECONDS",
        help=f"start: exit after SECONDS without client HTTP calls (default: ${IDLE_TIMEOUT_ENV}, else 0 = never)",
    )
    parser.add_argument(
        "--ask-before-act", action="store_true",
        help=f"start: hold sensitive actions for confirmation, locked on (also ${ASK_BEFORE_ACT_ENV}=1)",
    )
    args = parser.parse_args(argv)
    if args.command != "start" and args.idle_timeout is not None:
        parser.error("--idle-timeout only applies to start")
    if args.command != "start" and args.ask_before_act:
        parser.error("--ask-before-act only applies to start")
    if args.command == "status":
        try:
            status = bridge_control.status()
        except (bridge_control.httpx.HTTPError, ValueError) as exc:
            parser.exit(1, f"Cannot read Kitsune bridge status: {exc}\n")
        print(json.dumps(status if status is not None else {"status": "stopped"}, indent=2))
        return
    if args.command == "stop":
        with bridge_control.lifecycle_lock():
            error = bridge_control.stop()
        if error:
            parser.exit(1, error + "\n")
        print("Kitsune bridge stopped.")
        return
    idle_timeout = args.idle_timeout
    if idle_timeout is None:
        try:
            idle_timeout = _idle_timeout_seconds(os.environ.get(IDLE_TIMEOUT_ENV) or "0")
        except argparse.ArgumentTypeError as exc:
            parser.error(f"{IDLE_TIMEOUT_ENV}: {exc}")
    if args.ask_before_act or os.environ.get(ASK_BEFORE_ACT_ENV, "").lower() in {"1", "true", "yes", "on"}:
        _policy.update(ask=True, askLocked=True)

    # Separate from the short-lived lifecycle lock held by the spawning MCP
    # server. Keep this inode locked until all network servers have closed.
    fd = os.open(bridge_control.PID_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as pid_file:
        try:
            fcntl.flock(pid_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Kitsune bridge is already running.\n")
        pid_file.seek(0)
        pid_file.truncate()
        pid_file.write(str(os.getpid()))
        pid_file.flush()
        try:
            asyncio.run(main(idle_timeout))
        except KeyboardInterrupt:
            print(f"\n[{now()}] Shutting down...")
    if _idle_shutdown_lock is not None:
        _idle_shutdown_lock.close()


if __name__ == "__main__":
    run()
