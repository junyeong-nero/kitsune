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
import concurrent.futures
import contextvars
import itertools
from collections import Counter
from contextlib import asynccontextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock, Thread
from datetime import datetime, timezone

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
from .capabilities import ACTION_GROUPS, CAPS_HEADER, OPEN_ROUTES, ROUTE_GROUPS, caps_refusal, header_caps
from .task_modes import (
    GATEWAY_TOOL_HEADER, MODE_HEADER, PROTECT_USER_TABS_HEADER, permits_action, permits_route, task_mode,
)
from .version import BRIDGE_BUILD, PACKAGE_VERSION, SOURCE_ROOT, extension_manifest_hash, extension_source_hash
from .waits import (
    RESUME_DEADLINE_SECONDS, WORKFLOW_DEADLINE_SECONDS, actionability_wait_seconds, batch_deadline_seconds,
    command_wait_seconds, container_settle_seconds,
)

# ── Config ──
BRIDGE_VERSION = PACKAGE_VERSION
API_VERSION = "1"
HTTP_PORT = int(os.environ.get("KITSUNE_HTTP_PORT", "47651"))
WS_PORT = int(os.environ.get("KITSUNE_WS_PORT", "47652"))
EXTENSION_WS_PORT = 47652  # the default WS_URL in extension/background/background.js
# The unpacked extension a developer build is loaded from; a checkout's extension/ by default.
EXTENSION_SOURCE_DIR = Path(os.environ.get("KITSUNE_EXTENSION_DIR") or SOURCE_ROOT / "extension")
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
extension_info = {}  # {"version", "build", "sourceHash", "manifestHash"} from hello, "browser" from state
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
_audit_log = []  # list of {ts, action, params, ok, error, ms[, parent]}
_AUDIT_MAX = 2000
# {"call", "path"} of the container step running in this task; None for direct calls.
_audit_parent = contextvars.ContextVar("kitsune_audit_parent", default=None)
_audit_call_ids = itertools.count(1)
# Step records ({index, action, tabId?, status}) the running container's steps append to.
_container_steps = contextvars.ContextVar("kitsune_container_steps", default=None)
# ((owners, sequence), ...) of the enclosing parallel sequences; owners maps tab id -> sequence.
_tab_owners = contextvars.ContextVar("kitsune_tab_owners", default=())
# The step record of the container step running in this task (a parallel keeps its tab claims on it).
_running_step = contextvars.ContextVar("kitsune_running_step", default=None)
# The _ResumePlan of the container running in this task while browser_resume runs it; None otherwise.
_resume_plan = contextvars.ContextVar("kitsune_resume_plan", default=None)
# id(owners) -> (call, writers, owners) of each parallel in progress, so a resumed sequence can tell
# whether another call writes its tab.
_running_parallels = {}
_log_buffer = []  # mirrors bridge stdout lines
_LOG_MAX = 5000
# Ask-before-act is opt-in. askUrls are payment-like URL patterns it guards (case-insensitive).
DEFAULT_ASK_URLS = [r"checkout", r"payment", r"billing", r"/pay\b", r"purchase", r"/order"]
_task_mode = contextvars.ContextVar("kitsune_task_mode", default="act")
# The calling client's KITSUNE_PROTECT_USER_TABS (#199): its navigations leave user tabs alone.
_protect_user_tabs = contextvars.ContextVar("kitsune_protect_user_tabs", default=False)
# A container call's default tab (#232): its steps that name no tabId run there instead of the
# extension's active tab.
_default_tab = contextvars.ContextVar("kitsune_default_tab", default=None)
# The calling client's capability groups (#230); None (no header) leaves every group open.
_client_caps = contextvars.ContextVar("kitsune_client_caps", default=None)
# The tool a client's browser_tools_call runs (#227), which audit entries record.
_gateway_tool = contextvars.ContextVar("kitsune_gateway_tool", default=None)
_TOOL_NAME = _re.compile(r"browser_[a-z0-9_]{1,64}")
_PROTECTED_NAVIGATIONS = {"navigate", "goBack", "goForward"}


def _mode_error(operation):
    return {"error": f"{_task_mode.get()} mode refuses {operation}", "code": "mode_restricted",
            "retryable": False, "hint": "Restart this MCP client with KITSUNE_MODE=act to perform this action."}


def _ask_enabled():
    return _policy["ask"] or _task_mode.get() == "draft"


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
    "screenshot", "switchTab", "restoreActiveTab", "newTab", "closeTab", "trustedClick",
    "reloadExtension", "createWindow", "closeWindow", "focusWindow", "resizeWindow",
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
    "/api/resizeWindow": "/api/resize-window",
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


def _dequeue_scope(scope_name):
    _scheduler_queued[scope_name] = max(0, _scheduler_queued.get(scope_name, 1) - 1)
    if _scheduler_queued[scope_name] == 0:
        _scheduler_queued.pop(scope_name, None)


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
                _dequeue_scope(scope_name)
                queued = False
                _scheduler_active[scope_name] = action
                try:
                    return await coro_factory()
                finally:
                    _scheduler_active.pop(scope_name, None)
    finally:
        if queued:
            _dequeue_scope(scope_name)


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


TRUSTED_CLICK_RESTORE_DELAY = 0.3


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

    point_params = {"selector": selector, "x": x, "y": y, "click": True}
    if params.get("first"):
        point_params["first"] = True
    if params.get("tabId") is not None:
        point_params["tabId"] = params.get("tabId")
    # The extension shows a target tab that is not shown for the click (previousTabId) and
    # refuses with user_active when the user is using either tab.
    point = await send_to_extension("screenPoint", point_params)

    if "error" in point:
        return point

    try:
        focused = _focus_browser_window(title or point.get("title", ""), process) if focus else False
        clicked = _native_click_screen(point["screenX"], point["screenY"])
    finally:
        if point.get("previousTabId") is not None:
            # The OS delivers the click asynchronously; let it land before showing the user's tab again.
            await asyncio.sleep(TRUSTED_CLICK_RESTORE_DELAY)
            await send_to_extension("restoreActiveTab", {"tabId": point["previousTabId"], "from": point.get("tabId")})
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
# selectOption values are always redacted: the bridge only sees a selector/ref,
# and whether the <select> is sensitive (isSensitiveField) is known only in the page.
_AUDIT_SECRET_PARAMS = {
    "type": ("text",),
    "fill": ("value",),
    "setEditableContent": ("value",),
    "keypress": ("text",),
    "selectOption": ("value", "values"),
    "handleDialog": ("prompt_text",),
    "cookies": ("value",),
    "storageOp": ("value",),
    "clipboard": ("text",),
    "uploadFile": ("data",),
}

# JS bodies are dropped: "code" everywhere, plus the JS expressions that
# control-flow steps evaluate in the page.
_AUDIT_JS_PARAMS = {
    "if": ("condition",),
    "while": ("condition",),
    "syncBarrier": ("predicate",),
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
    js = _AUDIT_JS_PARAMS.get(action, ())
    out = {}
    for k, v in (params or {}).items():
        if k == "code" or k in js:  # don't log JS bodies
            continue
        if k in secret:
            out[k] = AUDIT_REDACTED
        elif action == "formFill" and k == "fields":
            # Keys are selectors/labels and stay; every value is typed content.
            out[k] = {f: AUDIT_REDACTED for f in v} if isinstance(v, dict) else AUDIT_REDACTED
        else:
            out[k] = scrub(v)
    return out


def _audit(action, params, ok, error, ms, **fields):
    """Record one command; fields adds entry keys, such as a history lookup's count and filters."""
    entry = {
        "ts": int(time.time() * 1000),
        "action": action,
        "params": _audit_params(action, params),
        "ok": bool(ok),
        "error": error,
        "ms": int(ms),
        **fields,
    }
    parent = _audit_parent.get()
    if parent is not None:
        entry["parent"] = parent
    if _gateway_tool.get():
        entry["via"] = {"gateway": "browser_tools_call", "tool": _gateway_tool.get()}
    _audit_log.append(entry)
    if len(_audit_log) > _AUDIT_MAX:
        del _audit_log[: len(_audit_log) - _AUDIT_MAX]


WRITE_ACTIONS = {
    "click", "type", "setEditableContent", "scroll", "hover", "fill",
    "navigate", "newTab", "closeTab", "switchTab", "executeJS", "trustedClick",
    "selectOption", "checkBox", "keypress", "doubleClick", "submitForm",
    "formFill", "drag", "cookies", "clipboard", "downloads",
    "clearBrowsingData", "intercept", "clickAndWaitNavigation",
    "pinTab", "muteTab", "duplicateTab", "reloadTabBrowser", "goBack", "goForward",
    "setZoom", "createWindow", "closeWindow", "resizeWindow", "moveTab", "detachTab",
    "storageOp", "reloadExtension", "wakeTab", "highlight", "clearHighlight",
    "uploadFile", "handleDialog", "media", "loadSession",
}


# Repeating these after an unknown outcome could duplicate a side effect (a submit,
# message, payment or deletion), so their timeouts and disconnects are not retryable.
_REPEAT_UNSAFE_ACTIONS = WRITE_ACTIONS | {"uploadCommit", "focusElement", "blurElement"}
# Nothing reached the page, but repeating cannot succeed: until the tab navigates (error_page),
# because the command names several targets (ambiguous_target) or its target selector failed
# to parse or validate before anything was dispatched (unsupported_selector), until the user
# approves closing their tab (user_tab), or until the user grants a browser permission
# (permission_required).
_PERSISTENT_FAILURE_CODES = {"error_page", "ambiguous_target", "unsupported_selector", "user_tab",
                             "permission_required"}


def _url_matches(pattern, url, flags=0):
    """re.search for a policy pattern; an invalid pattern matches nothing."""
    try:
        return bool(_re.search(pattern, url, flags))
    except _re.error:
        return False


def _check_policy(action, params):
    """Return error dict if action/url violates current policy, else None."""
    if not permits_action(_task_mode.get(), action):
        return _mode_error(action)
    if _task_mode.get() != "act":
        if (params or {}).get("confirmToken") is not None:
            return _mode_error("confirmation")
        if action == "navigate":
            from urllib.parse import urlsplit
            url = (params or {}).get("url", "")
            if urlsplit(url).scheme.lower() not in {"http", "https"} and url != "about:blank":
                return _mode_error("navigation outside http(s) or about:blank")
        if action == "setEditableContent" and (params or {}).get("format", "text") != "text":
            return _mode_error("HTML insertion")
    # Agent JavaScript (an if/while condition, waitForResult, syncBarrier) is refused like js.
    if _policy["readonly"] and (action in WRITE_ACTIONS or action in _ASK_JS_ACTIONS):
        # Read-only ops on cookies/storage/etc. are still allowed; tighten if needed.
        sub_op = params.get("op") if isinstance(params, dict) else None
        if action in ("cookies", "clipboard", "storageOp", "downloads") and sub_op in ("get", "read", "list", "snapshot"):
            return None
        return {"error": f"Read-only mode is active; refusing action: {action}"}
    url = params.get("url") if isinstance(params, dict) else None
    if url:
        for pat in _policy.get("deny", []):
            if _url_matches(pat, url):
                return {"error": f"URL denied by policy ({pat}): {url}"}
        allow = _policy.get("allow", [])
        if allow and not any(_url_matches(pat, url) for pat in allow):
            return {"error": f"URL not in allowlist: {url}"}
    return None


# ── History and bookmark lookup (#140, docs/api-reference.md#history-and-bookmarks) ──
# The extension reads history and bookmarks only after the user granted its optional permission
# on the options page; it applies the domain lists and time window sent here and redacts URLs.
# The bridge owns that scope: it starts from the environment, and browser_set_policy can only
# tighten it, so an agent never widens what it may read. These actions are not in
# BATCH_ACTION_MAP, so batch, parallel, broadcast, retry and workflows cannot send them unscoped.
HISTORY_ACTIONS = ("historySearch", "historyVisits", "bookmarksSearch")
HISTORY_ALLOW_ENV = "KITSUNE_HISTORY_ALLOW_DOMAINS"
HISTORY_DENY_ENV = "KITSUNE_HISTORY_DENY_DOMAINS"
HISTORY_MAX_DAYS_ENV = "KITSUNE_HISTORY_MAX_DAYS"
HISTORY_DEFAULT_MAX_DAYS = 30
HISTORY_DEFAULT_LIMIT = 20
HISTORY_LIMIT_MAX = 100  # also HISTORY_LIMIT_MAX in extension/background/history.js
_HISTORY_DOMAIN = _re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*$")
_DAY_MS = 86_400_000


def _history_domains(values, source):
    """Normalized domains from a list or a comma-separated string; ValueError names source."""
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{source} must be a list of domains")
    domains = []
    for value in values:
        domain = value.strip().lower().removeprefix("*.").strip(".")
        if not domain:
            continue
        if not _HISTORY_DOMAIN.match(domain):
            raise ValueError(f"{source}: {value!r} is not a domain (write example.com; subdomains match too)")
        if domain not in domains:
            domains.append(domain)
    return domains


def _history_max_days(value, source):
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if type(value) is not int or value < 1:
        raise ValueError(f"{source} must be a whole number of days, at least 1")
    return value


def _history_policy_from_env(environ):
    days = environ.get(HISTORY_MAX_DAYS_ENV, "").strip()
    return {
        "historyAllowDomains": _history_domains(environ.get(HISTORY_ALLOW_ENV, ""), HISTORY_ALLOW_ENV),
        "historyDenyDomains": _history_domains(environ.get(HISTORY_DENY_ENV, ""), HISTORY_DENY_ENV),
        "historyMaxDays": _history_max_days(days, HISTORY_MAX_DAYS_ENV) if days else HISTORY_DEFAULT_MAX_DAYS,
    }


_policy.update(_history_policy_from_env(os.environ))
# Every URL deny pattern and non-empty allow list browser_set_policy ever set. History results
# stay filtered by all of them, so loosening the URL policy for page actions never widens history.
_history_url_floor = {"deny": [], "allow": []}


def _covered_by(domain, domains):
    return any(domain == d or domain.endswith("." + d) for d in domains)


def _history_policy_update(body):
    """(updates, problems) for a set-policy body's history keys; ValueError for invalid values.

    The keys may only tighten the current scope: add deny domains, narrow the allow list
    (each entry within a current one), or lower the time window.
    """
    updates, problems = {}, []
    if "historyDenyDomains" in body:
        deny = _history_domains(body["historyDenyDomains"], "historyDenyDomains")
        dropped = [d for d in _policy["historyDenyDomains"] if d not in deny]
        if dropped:
            problems.append(f"historyDenyDomains cannot drop {', '.join(dropped)}")
        updates["historyDenyDomains"] = deny
    if "historyAllowDomains" in body:
        allow = _history_domains(body["historyAllowDomains"], "historyAllowDomains")
        current = _policy["historyAllowDomains"]
        if current and not allow:
            problems.append("historyAllowDomains cannot be emptied, which allows every domain")
        wider = [d for d in allow if current and not _covered_by(d, current)]
        if wider:
            problems.append(f"historyAllowDomains cannot add {', '.join(wider)}")
        updates["historyAllowDomains"] = allow
    if "historyMaxDays" in body:
        days = _history_max_days(body["historyMaxDays"], "historyMaxDays")
        if days > _policy["historyMaxDays"]:
            problems.append(f"historyMaxDays cannot rise above {_policy['historyMaxDays']}")
        updates["historyMaxDays"] = days
    return updates, problems


def _raise_history_url_floor(body):
    for pattern in body.get("deny") or []:
        if pattern not in _history_url_floor["deny"]:
            _history_url_floor["deny"].append(pattern)
    allow = list(body.get("allow") or [])
    if allow and allow not in _history_url_floor["allow"]:
        _history_url_floor["allow"].append(allow)


def _history_url_blocked(url):
    """Whether the URL policy floor hides url from history results (patterns as in _check_policy)."""
    return (any(_url_matches(pattern, url) for pattern in _history_url_floor["deny"])
            or any(not any(_url_matches(pattern, url) for pattern in allow) for allow in _history_url_floor["allow"]))


def _history_request(action, body, now_ms):
    """(extension params, audited params, filters) for a lookup, or an error dict first."""
    filters = {"allowDomains": list(_policy["historyAllowDomains"]),
               "denyDomains": list(_policy["historyDenyDomains"])}
    if action == "historyVisits":
        url = body.get("url")
        if not isinstance(url, str) or not _re.match(r"(?i)https?://", url):
            return {"error": "url must be an http(s) URL"}, None, None
        if _history_url_blocked(url):
            return {"error": f"URL denied by policy: {url}"}, None, None
        params, audited = {"url": url}, {"url": url}
    else:
        query = body.get("query", "")
        limit = body.get("limit", HISTORY_DEFAULT_LIMIT)
        if not isinstance(query, str):
            return {"error": "query must be a string"}, None, None
        if type(limit) is not int or not 1 <= limit <= HISTORY_LIMIT_MAX:
            return {"error": f"limit must be a whole number from 1 to {HISTORY_LIMIT_MAX}"}, None, None
        # Ask for the most results when the URL policy floor may drop some of them.
        params = {"query": query, "limit": HISTORY_LIMIT_MAX if any(_history_url_floor.values()) else limit}
        audited = {"query": query, "limit": limit}
    if action != "bookmarksSearch":
        window_start = now_ms - _policy["historyMaxDays"] * _DAY_MS
        since = body.get("since")
        if since is not None and (isinstance(since, bool) or not isinstance(since, (int, float))
                                  or not math.isfinite(since)):
            return {"error": "since must be epoch milliseconds"}, None, None
        if action == "historySearch":
            audited["since"] = since
        filters.update(maxDays=_policy["historyMaxDays"], startTime=int(max(window_start, since or 0)))
    return {**params, **filters}, audited, filters


async def _history_lookup(action, body):
    """Run one scoped history/bookmark lookup and audit it with its count, never result URLs."""
    t0 = time.time()
    params, audited, filters = _history_request(action, body, int(t0 * 1000))
    if audited is None:
        given = {key: body[key] for key in ("query", "url", "limit", "since") if key in body}
        _audit(action, given, False, params["error"], (time.time() - t0) * 1000, count=0)
        return params
    # A read of browser data, not of a tab: it needs no scheduler scope.
    result = await _send_to_extension_raw(action, params)
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        limit = audited["limit"]
        kept = [item for item in result["results"] if not _history_url_blocked(str(item.get("url", "")))]
        if len(kept) > limit:
            result["truncated"] = True
        result["results"] = kept[:limit]
        result["count"] = len(result["results"])
    if isinstance(result, dict) and "startTime" in filters and not result.get("error"):
        result["since"] = datetime.fromtimestamp(filters["startTime"] / 1000, timezone.utc).isoformat()
    error = result.get("error") if isinstance(result, dict) else "invalid extension reply"
    count = result.get("count", 0) if isinstance(result, dict) and not error else 0
    _audit(action, audited, not error, error, (time.time() - t0) * 1000, count=count,
           filters={**filters, "urlPolicy": any(_history_url_floor.values())})
    return result


# ── Ask-before-act (docs/api-reference.md#ask-before-act) ──
# Held actions do not run: they return confirmation_required with a token bound to the exact
# action, parameters and tab. The token goes back to the agent, so it enforces an explicit second
# step, not proof that a human approved.
ASK_BEFORE_ACT_ENV = "KITSUNE_ASK_BEFORE_ACT"
# A locked start leaves this file behind, so a later bridge started from an environment
# without KITSUNE_ASK_BEFORE_ACT=1 (stop, idle shutdown, outdated-bridge replacement) is
# locked too. Only deleting the file unlocks.
ASK_LOCK_FILE = os.path.join(DATA_DIR, "ask-before-act-locked")
CONFIRM_TOKEN_TTL = 120
_UNKNOWN_OUTCOME_TTL = 600
_ASK_JS_ACTIONS = {"executeJS", "waitForResult", "syncBarrier", "if", "while"}
_ASK_COOKIE_ACTIONS = {"cookies", "saveSession", "loadSession"}
# Held on every page and for every op, reads included: page storage often holds session
# tokens, the clipboard reaches outside the browser, and downloads write to disk or list
# local paths.
_ASK_DATA_ACTIONS = {
    "storageOp": ("storage", "reads or writes page storage (localStorage/sessionStorage)"),
    "clipboard": ("clipboard", "reads or writes the system clipboard"),
    "downloads": ("downloads", "starts, lists or cancels browser downloads"),
}
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
    if url:
        for pat in _policy.get("askUrls", []):
            if _url_matches(pat, url, _re.I):
                return pat
    return None


def _outcome_key(action, params):
    payload = {"action": action, "params": {k: v for k, v in (params or {}).items() if k != "confirmToken"}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _confirmation_binding(action, params, tab_id):
    return _outcome_key(action, {**(params or {}), "__tab": tab_id})


def _remember_outcome(action, params, result):
    """Under ask mode, remember writes whose outcome is unknown so repeating them is held."""
    if (not _ask_enabled() or not isinstance(result, dict) or result.get("retryable") is not False
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
    target = {key: params[key] for key in ("tabId", "selector", "ref", "key", "first") if params.get(key) is not None}
    if _task_mode.get() == "draft" and inspect:
        target["draftFill"] = True
        if isinstance(params.get("fields"), dict):
            target["fieldKeys"] = list(params["fields"])
    coords = params.get("coords") or ({"x": params["x"], "y": params["y"]} if "x" in params and "y" in params else None)
    if coords and not target.get("selector") and not target.get("ref"):
        target["coords"] = coords
    return await send_to_extension("describeTarget", {**target, "inspect": inspect}, timeout=10)


async def _held_reason(action, params):
    """Return a hold tuple, an ambiguity error, or None when ask mode permits the action."""
    reason = description = None
    url = params.get("url") if action in _ASK_NAVIGATION_ACTIONS else None
    if action in _ASK_JS_ACTIONS:
        reason, description = "javascript", "runs agent-supplied JavaScript in the page"
    elif action in _ASK_COOKIE_ACTIONS:
        reason, description = "cookies", "reads or writes browser cookies"
    elif action in _ASK_DATA_ACTIONS:
        reason, description = _ASK_DATA_ACTIONS[action]
    elif action == "submitForm":
        reason, description = "submit", "submits a form"
    elif pattern := _ask_url_match(url):
        reason, description = "payment_url", f"opens a payment-like URL ({pattern}): {url}"
    elif _outcome_key(action, params) in _unknown_outcomes:
        reason, description = "unknown_outcome", "repeats a write whose previous outcome is unknown"

    tab_id = url_now = None
    if action in _ASK_TAB_ACTIONS:
        submit_key = action == "keypress" and params.get("key") in {"Enter", "Return"}
        screen = action == "trustedClick" and (params.get("coordinateSpace") or params.get("space")) == "screen"
        draft_fill = _task_mode.get() == "draft" and action in {
            "type", "fill", "setEditableContent", "formFill", "selectOption", "checkBox"}
        inspect = ((action in _ASK_CLICK_ACTIONS or submit_key) and not screen) or draft_fill
        described = await _describe_target(params, inspect)
        if isinstance(described, dict) and described.get("code") == "ambiguous_target":
            return described
        if not isinstance(described, dict) or described.get("error"):
            # Fail closed: an action that cannot be inspected is held.
            error = described.get("error") if isinstance(described, dict) else described
            return (reason or "uninspectable_target",
                    description or f"acts on a target that could not be inspected ({error})",
                    _coerce_tab_id(params.get("tabId")), None)
        tab_id, url_now = described.get("tabId"), described.get("url")
        target = described.get("target") or {}
        if draft_fill and not isinstance(target.get("sensitive"), bool):
            return "uninspectable_target", "fills a target without sensitive-field inspection", tab_id, url_now
        if draft_fill and target.get("sensitive"):
            reason, description = "sensitive_field", "fills a sensitive field"
        if reason is None:
            verb = "presses Enter on" if submit_key else "clicks"
            if action in _ASK_PAGE_ACTIONS and (pattern := _ask_url_match(url_now)):
                reason, description = "payment_page", f"acts on a payment-like page ({pattern})"
            elif screen:
                reason, description = "uninspectable_target", "clicks screen coordinates that cannot be inspected"
            elif target.get("submits"):
                what = "presses Enter in a form field" if submit_key else "clicks a submit control"
                label = target.get("text") or target.get("tag") or ""
                reason, description = "submit_control", f"{what} ({label})" if label else what
            elif target.get("submitLabel"):
                reason, description = "submit_label", f"{verb} a button labeled like a submit or payment ({target.get('text')})"
            elif target.get("consequentialLabel"):
                reason, description = ("consequential_label", f"{verb} a button or link labeled like a send, "
                                       f"transfer, delete or account change ({target.get('text')})")
    if reason is None:
        return None
    return reason, description, tab_id, url_now or url


async def _ask_before_act(action, params, *, direct):
    """Return an ambiguity or confirmation_required error before dispatch, else None.

    Removes confirmToken from params. Tokens are only honored on direct calls, so batch,
    retry, parallel, broadcast and workflows never confirm an action.
    """
    token = params.pop("confirmToken", None) if isinstance(params, dict) else None
    if not _ask_enabled():
        return None
    held = await _held_reason(action, params)
    if held is None:
        return None
    if isinstance(held, dict):
        return held
    reason, description, tab_id, url = held
    if _task_mode.get() != "act":
        return {**_mode_error(description), "state": {"mode": _task_mode.get(), "reason": reason},
                "hint": "Nothing ran. Leave this sensitive action to the user, or restart with KITSUNE_MODE=act "
                        "and follow the ask-before-act policy."}
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


# ── User tabs (docs/api-reference.md#agent-owned-tabs) ──
# The extension closes only tabs (closeTab) and windows (closeWindow) the agent opened, and under
# a client's protectUserTabs navigates only those (navigate, goBack, goForward). Its user_tab
# refusal of a direct call carries a confirm token, issued and spent like ask-before-act's; the
# identical direct call with it sends userTabApproved, which callers can never set themselves.
def _user_tab_binding(action, params):
    return _confirmation_binding(
        action, {k: v for k, v in params.items() if k not in ("confirmToken", "userTabApproved")}, "user_tab")


def _take_user_tab_approval(action, params):
    """Spend a user_tab confirm token for this exact close; return why it was rejected, if it was.

    A token issued for anything else stays in params for ask-before-act.
    """
    token = params.get("confirmToken")
    entry = _confirm_tokens.get(token) if isinstance(token, str) else None
    binding = _user_tab_binding(action, params)
    if entry is None or entry["binding"] != binding:
        return None
    del params["confirmToken"]
    rejected = _spend_confirm_token(token, binding)
    if rejected is None:
        params["userTabApproved"] = True
        _audit(action, {**params, "confirmation": "accepted", "reason": "user_tab"}, True, None, 0)
    return rejected


def _offer_user_tab_approval(action, params, result, rejected=None):
    """Add a confirm token to a user_tab refusal of a direct close or protected navigation."""
    if not isinstance(result, dict) or result.get("code") != "user_tab":
        return result
    state = result.setdefault("state", {})
    if action in _PROTECTED_NAVIGATIONS and state.get("tabId") is not None:
        # Bound to the refused tab, so a call without tabId cannot carry it to whatever tab is active then.
        params = {**params, "tabId": state["tabId"]}
        hint = ("Nothing navigated: this session protects tabs the agent did not open. Open extra material "
                "with browser_tabs action=new and pass its tabId as tab_id, or ask the user to approve "
                f"navigating this tab, then repeat the call with tab_id={state['tabId']} and confirmToken "
                f"(MCP: confirm_token) within {CONFIRM_TOKEN_TTL}s. The token works once and only for this "
                "navigation.")
    else:
        hint = ("Nothing was closed: the agent did not open this tab. Leave it open, or ask the user to "
                "approve closing it, then repeat the identical call with confirmToken (MCP: confirm_token) "
                f"within {CONFIRM_TOKEN_TTL}s. The token works once and only for this close.")
    state.update(confirmToken=_issue_confirm_token(_user_tab_binding(action, params)),
                 expiresInSeconds=CONFIRM_TOKEN_TTL)
    if rejected:
        state["tokenRejected"] = rejected
    result["hint"] = hint
    return result


def _ask_locked_on_start(flag):
    """Return whether this bridge starts with ask mode locked on; persist a newly requested lock."""
    requested = flag or os.environ.get(ASK_BEFORE_ACT_ENV, "").lower() in {"1", "true", "yes", "on"}
    # lexists: a dangling symlink in place of the file still locks.
    if os.path.lexists(ASK_LOCK_FILE):
        return True
    if requested:
        try:
            fd = os.open(ASK_LOCK_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"Kitsune ask-before-act is locked on for every bridge using this directory "
                        f"(since {datetime.now().isoformat(timespec='seconds')}).\n"
                        f"To unlock: remove {ASK_BEFORE_ACT_ENV}=1 from every MCP server config, "
                        "delete this file, then run `kitsune-bridge stop`.\n")
        except OSError as exc:
            print(f"[{now()}] Warning: ask-before-act is locked for this bridge only; "
                  f"could not write {ASK_LOCK_FILE}: {exc}")
    return requested


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
    """Clear the short-lived read cache after browser/page mutations."""
    if action in WRITE_ACTIONS or action in ("elementScreenshot", "fullPageScreenshot"):
        _cache.clear()


# ── Orchestration: broadcast and syncBarrier ──

async def _broadcast_async(tab_ids, command, timeout=30):
    """Send the same command (minus tabId) to each tab in parallel."""
    if not tab_ids:
        return {"error": "broadcast requires at least one tabId"}
    action = command.get("action")
    if not action:
        return {"error": "broadcast command needs an action"}
    base_params = {k: v for k, v in command.items() if k != "action"}
    call = _audit_call()

    async def one(tid):
        # Route through the same logic batch uses so behavior is identical.
        cmd = {**base_params, "action": action, "tabId": tid}
        try:
            res = await asyncio.wait_for(_run_step(cmd, call, action="broadcast", tabId=tid), timeout=timeout)
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


def _session_failure(url, step, result):
    """One failed extension command of a session save/load, with its error details."""
    if not isinstance(result, dict):
        result = {"error": f"unexpected result: {result!r}"}
    failure = {"url": url, "step": step, "error": result.get("error") or "no result"}
    failure.update({key: result[key] for key in ("code", "state") if key in result})
    return failure


def _session_cookie_url(cookie):
    return ("https://" if cookie.get("secure") else "http://") + str(cookie.get("domain", "")).lstrip(".") + str(cookie.get("path", "/"))


def _session_policy_error(action, urls):
    """Apply the URL allow/deny policy to every URL a session save or load reads or writes."""
    for url in urls:
        policy_err = _check_policy(action, {"url": url})
        if policy_err:
            return policy_err
    return None


async def _save_session_async(name, urls):
    policy_err = _session_policy_error("saveSession", urls or [])
    if policy_err:
        return policy_err
    bundle = {"name": name, "saved": int(time.time()), "cookies": [], "storage": {}}
    failures = []
    # cookies for each url
    for u in (urls or []):
        r = await send_to_extension("cookies", {"op": "get", "url": u}, timeout=10)
        if isinstance(r, dict) and "cookies" in r:
            bundle["cookies"].extend(r["cookies"])
        else:
            failures.append(_session_failure(u, "cookies", r))
    # localStorage snapshot of each url, in a temporary tab
    if urls:
        for u in urls:
            # Open a temp tab for each origin, snapshot, close
            t = await send_to_extension("newTab", {"url": u})
            tid = t.get("tabId") if isinstance(t, dict) else None
            if not tid:
                failures.append(_session_failure(u, "newTab", t))
                continue
            # Wait briefly for page
            await asyncio.sleep(1)
            snap = await send_to_extension("storageOp", {"tabId": tid, "kind": "local", "op": "snapshot"}, timeout=10)
            if isinstance(snap, dict) and snap.get("ok"):
                bundle["storage"][u] = snap.get("items", {})
            else:
                failures.append(_session_failure(u, "storage", snap))
            await send_to_extension("closeTab", {"tabId": tid})
    # A partial bundle must not replace a saved session.
    if failures:
        return {"error": f"Session {name!r} not saved: {len(failures)} read(s) failed", "failures": failures}
    os.makedirs(_sessions_dir, mode=0o700, exist_ok=True)
    with open(_session_path(name), "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)
    return {"ok": True, "name": name, "path": _session_path(name), "cookies": len(bundle["cookies"]), "origins": len(bundle["storage"])}


async def _load_session_async(name):
    path = _session_path(name)
    if not os.path.isfile(path):
        return {"error": f"Session not found: {name}"}
    with open(path, "r", encoding="utf-8") as f:
        bundle = json.load(f)
    cookies, storage = bundle.get("cookies", []), bundle.get("storage") or {}
    policy_err = _session_policy_error("loadSession", [_session_cookie_url(c) for c in cookies] + list(storage))
    if policy_err:
        return policy_err
    set_count = 0
    failures = []
    for c in cookies:
        url = _session_cookie_url(c)
        try:
            r = await send_to_extension("cookies", {
                "op": "set", "url": url, "name": c.get("name"), "value": c.get("value"),
                "domain": c.get("domain"), "path": c.get("path"), "secure": c.get("secure"),
            }, timeout=10)
        except Exception as e:
            r = {"error": str(e)}
        if isinstance(r, dict) and not r.get("error"):
            set_count += 1
        else:
            failures.append(_session_failure(url, "cookies", r))
    storage_restored = 0
    for u, items in storage.items():
        t = await send_to_extension("newTab", {"url": u})
        tid = t.get("tabId") if isinstance(t, dict) else None
        if not tid:
            failures.append(_session_failure(u, "newTab", t))
            continue
        await asyncio.sleep(0.8)
        r = await send_to_extension("storageOp", {"tabId": tid, "kind": "local", "op": "restore", "value": items}, timeout=10)
        await send_to_extension("closeTab", {"tabId": tid})
        if isinstance(r, dict) and not r.get("error") and r.get("ok") is not False:
            storage_restored += 1
        else:
            failures.append(_session_failure(u, "storage", r))
    result = {"ok": True, "name": name, "cookies_set": set_count, "origins_restored": storage_restored}
    if failures:
        # Restoring is idempotent, so repeating the load after fixing the cause is safe.
        del result["ok"]
        result = {"error": f"Session {name!r} partly restored: {len(failures)} write(s) failed",
                  **result, "failures": failures}
    return result


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


def _active_keep_alive_tabs():
    return [tid for tid, t in _keep_alive_tasks.items() if not t.done()]


def _workflow_names():
    if not os.path.isdir(WORKFLOW_DIR):
        return []
    return [f[:-3] for f in os.listdir(WORKFLOW_DIR) if f.endswith('.md')]


def _parse_workflow(filepath, variables=None):
    """Parse a workflow .md file into a list of batch commands."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return _parse_workflow_text(f.read(), variables)


def _parse_workflow_text(content, variables=None):
    variables = variables or {}
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
                    # Extensions before #34's build check omit sourceHash; before #118, manifestHash.
                    for key in ("sourceHash", "manifestHash"):
                        if key in data:
                            extension_info[key] = data[key]
                    _log(f"Extension {extension_info['version']} ({extension_info['build']} build)")
                continue
            if data.get("type") == "state":
                if extension_ws is websocket:
                    extension_info["browser"] = {"windows": data.get("windows"), "allSites": data.get("allSites")}
                continue

            if cmd_id and cmd_id in pending_commands:
                future = pending_commands[cmd_id]["future"]
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
            future = entry["future"]
            if entry.get("ws") is websocket:
                if not future.done():
                    future.set_result(_disconnected_error("Extension disconnected", entry.get("action")))
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


def _timeout_error(action, timeout):
    return _action_error(
        f"Command timed out after {timeout}s", "timeout",
        retryable=_safe_to_repeat(action),
        hint="No reply arrived in time, so the command's effect is unknown; check the page "
             "with browser_snapshot before repeating it.",
        timeoutSeconds=timeout,
    )


def _connection_diagnosis():
    """browser_status diagnosis from bridge state (docs/api-reference.md#connection-diagnosis)."""
    if extension_ws is None:
        # An extension that connected once knows the port; one that never did may use another.
        extension_port = None if extension_ws_generation else _extension_source_ws_port()
        if WS_PORT != (extension_port or EXTENSION_WS_PORT) and not extension_ws_generation:
            uses = (f"the extension in {EXTENSION_SOURCE_DIR} connects to {extension_port} (WS_URL in "
                    "background/background.js)" if extension_port else
                    f"the extension connects to {EXTENSION_WS_PORT} unless WS_URL in "
                    "extension/background/background.js was changed to match")
            return {"code": "port_conflict",
                    "hint": f"The bridge waits for the extension on port {WS_PORT} (KITSUNE_WS_PORT), but {uses}. "
                            "Set KITSUNE_WS_PORT to the extension's port, or change WS_URL and reload the "
                            "developer build.",
                    "state": {"port": WS_PORT, "reason": "extension_ws_port",
                              "extensionPort": extension_port or EXTENSION_WS_PORT}}
        return {"code": "extension_disconnected",
                "hint": "Open the browser profile where Kitsune is installed and check that Kitsune is enabled in "
                        "about:addons (load a developer build again in about:debugging after a browser restart). "
                        "The extension reconnects within 15 s of a bridge start; check browser_status again."}
    version = extension_info.get("version")
    if version != BRIDGE_VERSION:
        return {"code": "extension_version_mismatch",
                "hint": f"Install the Kitsune extension for bridge {BRIDGE_VERSION} (about:addons; reload a "
                        "developer build in about:debugging), or update the server if the extension is newer. "
                        "Then reload open tabs.",
                "state": {"extensionVersion": version, "bridgeVersion": BRIDGE_VERSION}}
    stale = _stale_developer_build()
    if stale:
        return {"code": "extension_version_mismatch",
                "hint": f"The developer build in the browser differs from {EXTENSION_SOURCE_DIR} (edited but not "
                        "reloaded): reload it in about:debugging → This Firefox → Kitsune, then reload open tabs. "
                        "Set KITSUNE_EXTENSION_DIR if the browser loads it from another directory.",
                "state": {"extensionVersion": version, "bridgeVersion": BRIDGE_VERSION,
                          "reason": "stale_developer_build", **stale}}
    browser = extension_info.get("browser") or {}
    if browser.get("windows") == 0:
        return {"code": "browser_window_unavailable", "hint": _BROWSER_WINDOW_HINT, "state": {"windows": 0}}
    if browser.get("allSites") is False:
        return {"code": "host_permission_denied",
                "hint": "Kitsune may not read or change websites: allow its site access in about:addons → "
                        "Kitsune → Permissions.",
                "state": {"reason": "not_granted"}}
    return {"code": "connected"}


def _stale_developer_build():
    """The fingerprints that differ when a connected developer build differs from EXTENSION_SOURCE_DIR.

    Scripts (sourceHash) and manifest.json (manifestHash) are compared separately. Read from disk
    at every diagnosis: the question is whether the files were edited since the browser loaded
    them. Unknown on either side (an older extension, the signed build, no unpacked extension next
    to the bridge) is never a mismatch.
    """
    if extension_info.get("build") != "dev":
        return None
    stale = {}
    for key, name, fingerprint in (("sourceHash", "SourceHash", extension_source_hash),
                                   ("manifestHash", "ManifestHash", extension_manifest_hash)):
        loaded = extension_info.get(key)
        if not isinstance(loaded, str):
            continue
        on_disk = fingerprint(EXTENSION_SOURCE_DIR)
        if on_disk is not None and on_disk != loaded:
            stale.update({f"extension{name}": loaded[:16], f"expected{name}": on_disk})
    return stale or None


# `const WS_URL = 'ws://host:port'` in background.js
_WS_URL_PORT = _re.compile(r"""\bconst\s+WS_URL\s*=\s*['"`]wss?://[^'"`/]*:(\d+)""")


def _extension_source_ws_port():
    """The port WS_URL names in EXTENSION_SOURCE_DIR's background.js; None if unreadable.

    Read at each diagnosis like _stale_developer_build. Only a developer build loaded from that
    directory is known to use it; the signed build always uses EXTENSION_WS_PORT.
    """
    try:
        source = (EXTENSION_SOURCE_DIR / "background" / "background.js").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    match = _WS_URL_PORT.search(source)
    return int(match[1]) if match and 0 < int(match[1]) < 65536 else None


# Matches the extension's browser_window_unavailable hint.
_BROWSER_WINDOW_HINT = ("Open a normal browser window in the profile where Kitsune is installed (private windows "
                        "need Run in Private Windows in about:addons), or pass tab_id.")


def _disconnected_error(message, action, sent=True):
    if not sent and extension_ws is None:
        diagnosis = _connection_diagnosis()
        return _action_error(message, diagnosis["code"], retryable=True, hint=diagnosis["hint"],
                             connected=False, **diagnosis.get("state", {}))
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
    if action in _PROTECTED_NAVIGATIONS:
        # Only the calling client's setting decides whether its navigations are protected.
        params = {k: v for k, v in (params or {}).items() if k != "protectUserTabs"}
        if _protect_user_tabs.get():
            params["protectUserTabs"] = True
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
    post_action_snapshot = os.environ.get("KITSUNE_POST_ACTION_SNAPSHOT", "1").lower()
    params.setdefault("includeSnapshot", post_action_snapshot not in {"0", "false", "no", "off"})
    if post_action_snapshot == "text":
        params.setdefault("snapshotText", True)
    elif post_action_snapshot == "full":
        params.setdefault("snapshot", "full")
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
        # An extension of another version may not know the action; nothing ran.
        if isinstance(result, dict) and str(result.get("error", "")).startswith("Unknown action:"):
            diagnosis = _connection_diagnosis()
            if diagnosis["code"] == "extension_version_mismatch":
                return _action_error(result["error"], diagnosis["code"], retryable=True,
                                     hint=diagnosis["hint"], **diagnosis["state"])
        # The extension cannot tell reads from writes; repeating a read is always safe.
        if (isinstance(result, dict) and result.get("code") and _safe_to_repeat(action)
                and result["code"] not in _PERSISTENT_FAILURE_CODES):
            result["retryable"] = True
        return result
    except asyncio.TimeoutError:
        return _timeout_error(action, timeout)
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
_OUTCOME_CONDITIONS = ("url", "url_changed", "new_tab", "selector", "text", "text_gone")
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
    unknown = set(expect) - {*_OUTCOME_CONDITIONS, "gone", "value", "value_changed", "timeout"}
    if unknown:
        return None, "Unknown expect keys: " + ", ".join(sorted(unknown))
    kinds = [key for key in _OUTCOME_CONDITIONS if expect.get(key) is not None]
    if len(kinds) != 1:
        return None, "expect needs exactly one of " + ", ".join(_OUTCOME_CONDITIONS)
    kind = kinds[0]
    if kind in ("url_changed", "new_tab"):
        if expect[kind] is not True:
            return None, f"expect.{kind} must be true"
    elif not isinstance(expect[kind], str) or not expect[kind]:
        return None, f"expect.{kind} must be a non-empty string"
    gone, value = expect.get("gone", False), expect.get("value")
    value_changed = expect.get("value_changed", False)
    if not isinstance(gone, bool):
        return None, "expect.gone must be a boolean"
    if not isinstance(value_changed, bool):
        return None, "expect.value_changed must be a boolean"
    if (gone or value is not None or value_changed) and kind != "selector":
        return None, "expect.gone, expect.value and expect.value_changed require expect.selector"
    if [gone, value is not None, value_changed].count(True) > 1:
        return None, "expect.gone, expect.value and expect.value_changed are mutually exclusive"
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
    if value_changed:
        spec["value_changed"] = True
    return spec, None


def _outcome_budget_seconds(spec):
    """Time to allow beyond dispatch: the bounded wait plus the tab reads around it."""
    return spec["timeout"] / 1000 + 45


async def _opened_tabs(tab_id):
    """Tabs that tab_id opened since its last page action started ([] if unknown)."""
    r = await send_to_extension("getOpenedTabs", {"tabId": tab_id}, timeout=10)
    return (r.get("opened_tabs") or []) if isinstance(r, dict) else []


async def _tab_state(tab_id):
    """Current url/title of a tab, for outcome checks and failure state."""
    tabs = await send_to_extension("getTabs", {}, timeout=10)
    if not isinstance(tabs, dict) or tabs.get("error"):
        return {"tabId": tab_id}
    tab = next((t for t in tabs.get("tabs", []) if t.get("id") == tab_id), None)
    if tab is None:
        return {"tabId": tab_id, "closed": True}
    return {"tabId": tab_id, "url": tab.get("url"), "title": tab.get("title")}


async def _query_value(tab_id, selector):
    """Read the first match's value; returns the query reply and its first item (or None)."""
    r = await send_to_extension("query", {"tabId": tab_id, "selector": selector,
                                         "fields": ["value"], "limit": 1}, timeout=10)
    return r, (r.get("items") or [None])[0]


async def _check_outcome(spec, tab_id, before, remaining_ms):
    """One observation bounded by remaining_ms; returns (met, observed, final).

    before is the pre-dispatch observation: the tab URL for url_changed, or the
    first match's query item (None if absent) for value_changed.

    Page reads are single checks that the caller polls: a wait inside the page runs on
    its timers, which the browser throttles in background tabs, so it can end long
    after remaining_ms.
    """
    wait_s = remaining_ms / 1000 + 5
    waited = True  # a wait that ends unmet has used up the remaining time
    if "url" in spec or "url_changed" in spec:
        # Tab metadata survives document replacement, unlike content-script reads.
        pattern = spec.get("url") or "^(?!%s$)" % _re.escape(before or "")
        r = await send_to_extension("waitForUrl", {"tabId": tab_id, "pattern": pattern,
                                                  "timeout": remaining_ms}, timeout=wait_s)
        observed = {"source": "tab", "url": r.get("url")}
        met = r.get("ok") is True
    elif "new_tab" in spec:
        # One read per poll: the extension records the tabs this tab opened.
        waited = False
        r = await send_to_extension("getOpenedTabs", {"tabId": tab_id}, timeout=10)
        opened = r.get("opened_tabs") or []
        observed, met = {"source": "tabs", "opened_tabs": opened}, bool(opened)
    elif "text" in spec or "text_gone" in spec:
        # A 0 ms waitForText is a single read of the visible text.
        waited = False
        r = await send_to_extension("waitForText", {"tabId": tab_id, "text": spec.get("text"),
                                                   "text_gone": spec.get("text_gone"),
                                                   "timeout": 0}, timeout=10)
        met = r.get("ok") is True
        observed = {"source": "page", "textVisible": met if "text" in spec else not met}
    elif "value" in spec or "value_changed" in spec:
        waited = False
        r, item = await _query_value(tab_id, spec["selector"])
        if item is None:
            observed, met = {"source": "page", "found": False}, False
        elif item.get("redacted"):
            # Sensitive values are never read back, so this outcome cannot be verified.
            return False, {"source": "page", "found": True, "redacted": True,
                           "hasValue": item.get("hasValue")}, True
        elif "value_changed" in spec:
            # A field absent before dispatch has changed once it is present.
            previous = before.get("value") if before else None
            observed = {"source": "page", "found": True, "value": item.get("value"),
                        "before": previous}
            met = before is None or item.get("value") != previous
        else:
            observed = {"source": "page", "found": True, "value": item.get("value")}
            met = item.get("value") == spec["value"]
    else:
        # A 0 ms waitForElement is a single visibility check.
        waited = False
        r = await send_to_extension("waitForElement", {"tabId": tab_id, "selector": spec["selector"],
                                                      "timeout": 0}, timeout=10)
        visible = r.get("found") is True
        observed = {"source": "page", "visible": visible}
        met = r.get("found") is False if spec.get("gone") else visible
    # Waits report a timeout as ok:false with an error; anything else failed to observe.
    if r.get("error") and r.get("ok") is not False:
        observed = {"source": observed["source"], "error": r["error"],
                    **({"code": r["code"]} if r.get("code") else {})}
        return False, observed, r.get("code") not in _OUTCOME_TRANSIENT_CODES
    return met, observed, waited


async def _observe_outcome(spec, tab_id, before, snapshot):
    """Poll until the expected outcome is observed or spec['timeout'] ms pass."""
    start = time.monotonic()
    # Reuse the post-action snapshot: text already in it needs no further read.
    if "text" in spec and isinstance(snapshot, str) and spec["text"] in snapshot:
        return {"met": True, "observed": {"source": "snapshot", "textVisible": True}, "elapsed": 0}
    deadline = start + spec["timeout"] / 1000
    while True:
        remaining_ms = max(0, math.ceil((deadline - time.monotonic()) * 1000))
        met, observed, final = await _check_outcome(spec, tab_id, before, remaining_ms)
        if met or final or time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(OUTCOME_POLL_SECONDS, max(0, deadline - time.monotonic())))
    return {"met": met, "observed": observed, "elapsed": int((time.monotonic() - start) * 1000)}


async def _click_target_visible(tab_id, params, result):
    """Whether the clicked element is still visible: the post-action snapshot's target_ref,
    else, when no snapshot was taken, the request's ref or selector."""
    target = result.get("target_ref")
    if not target and "snapshot" not in result:
        target = params.get("ref") or params.get("selector")
    if not target:
        return False
    r = await send_to_extension("waitForElement", {"tabId": tab_id, "selector": target,
                                                  "timeout": 1, "pollInterval": 100}, timeout=6)
    return r.get("found") is True


async def _poll_page_wait(action, params):
    """Run a waitForText/waitForElement wait as single page reads polled until its timeout.

    A wait inside the page runs on its timers, which the browser throttles in background
    tabs, so it can end long after its timeout. A reply other than a timed-out read (met,
    or an error) ends the wait; an unmet wait reports the requested timeout and elapsed.
    """
    timeout_ms = params.get("timeout", 10000)
    poll_s = (params.get("pollInterval") or 100) / 1000
    start = time.monotonic()
    deadline = start + timeout_ms / 1000
    while True:
        remaining_s = max(0, deadline - time.monotonic())
        r = await send_to_extension(action, {**params, "timeout": 0}, timeout=remaining_s + 5)
        if not isinstance(r, dict) or r.get("ok") is not False or time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(poll_s, max(0, deadline - time.monotonic())))
    if isinstance(r, dict) and r.get("ok") is False:
        r["elapsed"] = int((time.monotonic() - start) * 1000)
        r["error"] = _re.sub(r"^Timed out after 0ms", f"Timed out after {timeout_ms}ms", r.get("error", ""))
    elif isinstance(r, dict) and r.get("ok") is True:
        r["elapsed"] = int((time.monotonic() - start) * 1000)
    return r


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
    before = None
    if "url_changed" in spec:
        before = (await _tab_state(tab_id)).get("url")
        if before is None:
            return {"error": f"Cannot read the URL of tab {tab_id} before dispatch"}
    elif "value_changed" in spec:
        r, before = await _query_value(tab_id, spec["selector"])
        if r.get("error"):
            return {"error": f"Cannot read the value of {spec['selector']} before dispatch: {r['error']}"}
        if before and before.get("redacted"):
            return {"error": f"{spec['selector']} is a sensitive field whose value is never read back, "
                              "so expect.value_changed cannot be verified"}
    result = await send_to_extension(action, params, timeout=timeout)
    if not isinstance(result, dict) or result.get("error") or result.get("ok") is False:
        return result
    outcome = await _observe_outcome(spec, tab_id, before, result.get("snapshot"))
    expected = dict(spec)
    if outcome["observed"].get("redacted"):
        expected.pop("value", None)  # never echo a value meant for a sensitive field
    result["dispatched"] = True
    result["outcome"] = {"expected": expected, **outcome}
    if outcome["met"]:
        return result
    state = await _tab_state(tab_id)
    result["outcome"]["state"] = state
    hint = ("The command was dispatched but the expected outcome did not happen; inspect the page "
            "with browser_snapshot before acting again.")
    # An outcome that happened in a tab the action opened is not a no-op.
    opened = [] if "new_tab" in spec else await _opened_tabs(tab_id)
    if opened:
        result["opened_tabs"] = opened
        ids = ", ".join(str(t.get("tabId")) for t in opened)
        tabs = "tab" if len(opened) == 1 else "tabs"
        hint = (f"The command was dispatched and opened {tabs} {ids}, where the outcome may have happened; "
                f"inspect it with browser_snapshot tab_id={opened[0].get('tabId')} instead of "
                "repeating the action.")
    elif (result.get("warning") or {}).get("code") == "popup_blocked":
        hint = result["warning"]["hint"]
    elif action == "click" and await _click_target_visible(tab_id, params, result):
        hint += (" The clicked element is still visible: the page may accept only trusted input, "
                 "and browser_trusted_click (needs the browse capability) is the alternative.")
    result.update(_action_error(
        f"Expected outcome not observed within {spec['timeout']}ms", "outcome_not_observed",
        retryable=_safe_to_repeat(action), hint=hint, **state,
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
    data = body.get("data")
    if data is not None and not (isinstance(data, dict) and all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items())):
        return {"error": "data must map MIME types to strings"}
    if not paths and not (data and body.get("mode") == "drop"):
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
    commit = {"tabId": tab_id, "uploadId": uid, "selector": sel, "files": files, "mode": mode,
              "first": bool(body.get("first"))}
    if data:
        commit["data"] = data
    r = await send_to_extension("uploadCommit", commit, timeout=60)
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


# Action name -> extension action name (for simple forwarding); most keep their name.
BATCH_ACTION_MAP = {
    'js': 'executeJS', 'pageInfo': 'getPageInfo', 'pageText': 'getPageText',
    'pageTextByTabId': 'getPageTextFromTab', 'tabs': 'getTabs', 'forms': 'getFormFields',
    'dom': 'getAccessibilityTree', 'snapshot': 'getSnapshot',
    **{action: action for action in (
        'navigate', 'newTab', 'closeTab', 'switchTab', 'click', 'type', 'setEditableContent', 'fill',
        'scroll', 'hover', 'find', 'highlight', 'wakeTab', 'reloadExtension', 'pinTab', 'muteTab',
        'duplicateTab', 'reloadTabBrowser', 'goBack', 'goForward', 'getZoom', 'setZoom', 'getWindows',
        'createWindow', 'closeWindow', 'focusWindow', 'resizeWindow', 'moveTab', 'detachTab', 'elementScreenshot',
        'fullPageScreenshot', 'cookies', 'clipboard', 'downloads', 'clearBrowsingData', 'intercept',
        'clickAndWaitNavigation', 'query', 'getHTML', 'getLinks', 'getImages', 'getMeta',
        'getStructuredData', 'getBounds', 'getComputedStyle', 'getReadability', 'getMarkdown',
        'selectOption', 'checkBox', 'focusElement', 'blurElement', 'keypress', 'doubleClick',
        'submitForm', 'formFill', 'media', 'drag', 'waitForUrl', 'waitForTitle', 'waitForNetworkIdle',
        'captureNetwork', 'watchConsole', 'consoleLogs', 'storageOp', 'getIframes', 'explainSelector',
        'fullPageMetrics',
    )},
}


def _audit_call():
    """Return the container call nested steps are grouped under; a top-level container starts one."""
    return _audit_parent.get() or {"call": next(_audit_call_ids), "path": []}


# ── Container progress and tab ownership (docs/api-reference.md#container-progress) ──

# Worst first: a container step takes the first of these that it or a nested step has.
_STEP_STATUSES = ("unknown", "held", "failed", "skipped", "ok")
# Actions that act on no page tab, so a parallel step may omit tabId; containers are checked by
# their nested steps. screenshot captures the visible tab and ignores tabId.
_TABLESS_ACTIONS = {
    "sleep", "newTab", "getTabs", "getWindows", "createWindow", "closeWindow", "focusWindow",
    "cookies", "clipboard", "downloads", "clearBrowsingData", "intercept", "reloadExtension",
    "screenshot", "broadcast", "syncBarrier", "parallel", "sequence", "retry", "try",
}
# Their condition is JavaScript run on their tab (executeJS, a write), so like js they claim it.
_CONDITION_ACTIONS = {"if", "while"}
_UNFINISHED = object()


def _explicit_tab_id(value):
    """The tab id value names (a positive integer or its digits), else None."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if type(value) is int and value > 0 else None


def _container_default_tab(body):
    """(default tab, None) from a container request's top-level tabId, else (None, 400 refusal)."""
    raw = body.pop("tabId", None)
    tab_id = _explicit_tab_id(raw)
    if raw is not None and tab_id is None:
        return None, (400, _action_error(f"tabId {raw!r} is not a tab id", "invalid_tab_id", retryable=False,
                                         hint="Nothing was dispatched. Use a tab id from browser_tabs.",
                                         tabId=raw))
    return tab_id, None


def _tab_required_error(action, sequence, **state):
    return _action_error(
        f'Sequence {sequence}: "{action}" inside parallel requires explicit tabId to avoid race conditions',
        "tab_required", retryable=True,
        hint="Nothing was dispatched. Give every step that acts on a tab its tabId; browser_tabs lists them.",
        sequence=sequence, **state)


def _tab_owned_error(action, sequence, tab_id, owner, **state):
    return _action_error(
        f'Sequence {sequence}: "{action}" on tab {tab_id} inside parallel conflicts with sequence {owner}, '
        "which also writes that tab",
        "tab_owned", retryable=True,
        hint=("Nothing was dispatched. Let one sequence write each tab; put every write to a tab in its sequence. "
              "An if/while condition runs JavaScript on its tab, so it counts as a write."),
        sequence=sequence, tabId=tab_id, owner=owner, **state)


def _nested_commands(cmd):
    """cmd and every command nested in it (sequence/if/while/try blocks, retry, parallel)."""
    if not isinstance(cmd, dict):
        return []
    action = cmd.get("action")
    if action == "parallel":
        nested = [c for seq in cmd.get("sequences") or [] for c in seq]
    elif action == "retry":
        nested = [cmd.get("command")]
    else:
        blocks = {"sequence": ("do",), "while": ("do",), "try": ("do", "catch"), "if": ("then", "else")}
        nested = [c for block in blocks.get(action, ()) for c in cmd.get(block) or []]
    return [cmd, *(c for n in nested for c in _nested_commands(n))]


def _parallel_tab_problem(sequences, writers):
    """Return why parallel sequences could act on a tab they do not own, before any step runs.

    Every step that acts on a tab must name it, so a focus change cannot redirect it to another
    sequence's tab, and no tab may be written by two sequences. Tab ids substituted at run time
    (${$N.tabId}) are checked by _check_step_tab when their step runs. Fills writers (tab id ->
    sequence) with the tabs the steps name.
    """
    for i, seq in enumerate(sequences):
        for j, cmd in enumerate(seq):
            for step in _nested_commands(cmd):
                action = _effective_action(step)
                if not isinstance(action, str):
                    continue
                raw = step.get("tabId")
                if action in _TABLESS_ACTIONS or (isinstance(raw, str) and "${" in raw):
                    continue
                tab_id = _explicit_tab_id(raw)
                if tab_id is None:
                    return _tab_required_error(action, i, index=j, tabId=raw)
                if _claims_tab(action) and writers.setdefault(tab_id, i) != i:
                    return _tab_owned_error(action, i, tab_id, writers[tab_id], index=j)
    return None


def _claims_tab(action):
    """Whether a parallel step owns its tab: a write, or an if/while whose condition runs on it."""
    return action in _REPEAT_UNSAFE_ACTIONS or action in _CONDITION_ACTIONS


def _check_step_tab(action, params):
    """Return an error when a container step names no usable tab, else None.

    The extension sends a step with a falsy tabId to the active tab, so a named tabId that is not
    a tab id (0, "", "null", an unresolved ${$N.tabId}) is refused; "12" becomes 12. A step that
    acts on a tab and names none runs on the call's default tab, if it has one. Inside a parallel
    sequence, a step that acts on a tab must name it, and a write or an if/while condition claims
    the tab for its sequence.
    """
    raw = params.get("tabId")
    chain = _tab_owners.get()
    if raw is None and not chain and action not in _TABLESS_ACTIONS and _default_tab.get() is not None:
        raw = params["tabId"] = _default_tab.get()
    tab_id = _explicit_tab_id(raw)
    if raw is not None:
        if tab_id is None:
            return _action_error(
                f'tabId {raw!r} is not a tab id; "{action}" was not sent to the active tab instead',
                "invalid_tab_id", retryable=True,
                hint=("Nothing was dispatched. Use a tab id from browser_tabs, or check the earlier "
                      "result a ${$N.tabId} substitution reads."),
                tabId=raw)
        params["tabId"] = tab_id
    if not chain or action in _TABLESS_ACTIONS:
        return None
    sequence = chain[-1][1]
    if tab_id is None:
        return _tab_required_error(action, sequence)
    if _claims_tab(action):
        for owners, claimant in chain:
            if owners.setdefault(tab_id, claimant) != claimant:
                return _tab_owned_error(action, sequence, tab_id, owners[tab_id])
    return None


def _result_status(result):
    """ok, failed, held or unknown (a write whose effect is unknown) for one command's result."""
    if _is_held(result):
        return "held"
    if not isinstance(result, dict):
        return "ok"
    if result.get("error"):
        if result.get("retryable") is False and result.get("code") not in _PERSISTENT_FAILURE_CODES:
            return "unknown"
        return "failed"
    return "failed" if result.get("ok") is False else "ok"


def _worst_status(statuses):
    statuses = set(statuses)
    return next((s for s in _STEP_STATUSES if s in statuses), "ok")


class _StepRecord(dict):
    """A progress record. Its result, nested records and (for a parallel) tab owners are attributes,
    so they stay out of the response but a checkpoint can resume from them."""
    result = None
    nested = ()
    owners = None

    def copy_with(self, **fields):
        record = _StepRecord({**self, **fields})
        record.result, record.nested, record.owners = self.result, self.nested, self.owners
        return record


def _step_record(cmd, status="running", **position):
    step = _StepRecord(position, action=cmd.get("action") if isinstance(cmd, dict) else None)
    if isinstance(cmd, dict) and cmd.get("tabId") is not None:
        step["tabId"] = cmd["tabId"]
    step["status"] = status
    return step


def _finish_step(step, cmd, result, nested):
    """Set a step's status from its result and its nested steps; a cancelled step is unknown."""
    step.result, step.nested = result, nested
    own = "unknown" if result is _UNFINISHED else _result_status(result)
    if own == "ok" and step["action"] == "retry":
        nested = []  # attempts that failed before one succeeded were safe to repeat
    step["status"] = _worst_status([own, *(s["status"] for s in nested)])
    if step["action"] == "parallel" and isinstance(cmd.get("sequences"), list):
        ran = {(s.get("sequence"), s.get("index")): s for s in nested}
        step["sequences"] = [
            [ran.get((i, j)) or _step_record(c, "skipped", sequence=i, index=j) for j, c in enumerate(seq)]
            for i, seq in enumerate(cmd["sequences"]) if isinstance(seq, list)]


def _container_progress(commands, steps):
    """progress (one record per command; skipped when it never started) and overall status."""
    ran = {s["index"]: s for s in steps}
    progress = [ran.get(i) or _step_record(c, "skipped", index=i) for i, c in enumerate(commands)]
    return {"progress": progress, "status": _worst_status(s["status"] for s in progress)}


# What each status means for the user, in the order the summary lists them; ok needs no list.
_SUMMARY_DETAILS = (
    ("unknown", "Unknown, may have taken effect (check the page before repeating)"),
    ("held", "Held for confirmation, not run (confirm each with a direct call)"),
    ("failed", "Failed, safe to repeat after its hint"),
    ("skipped", "Skipped, never started"),
)
_SUMMARY_LIST_MAX = 5


def _summary_commands(records, prefix=""):
    """(label, record) per command; a parallel that started is replaced by its sequences' commands."""
    for record in records:
        label = prefix + (f"sequence {record['sequence']} " if "sequence" in record else "") + f"step {record['index']}"
        nested = [step for seq in record.get("sequences", []) for step in seq]
        if any(step["status"] != "skipped" for step in nested):
            # browser_parallel sends one parallel command; its sequences need no "step 0, " prefix.
            yield from _summary_commands(nested, "" if len(records) == 1 and not prefix else label + ", ")
        else:
            yield label, record


def _container_summary(progress, deadline=None):
    """One human-readable paragraph of a container's progress, additive to progress itself."""
    commands = list(_summary_commands(progress))
    counts = Counter(record["status"] for _, record in commands)
    head = ", ".join(f"{counts[status]} {status}" for status in ("ok", *(s for s, _ in _SUMMARY_DETAILS))
                     if counts[status])
    parts = [f"Stopped at its {deadline:g}s deadline."] if deadline is not None else []
    parts.append(f"{len(commands)} command{'' if len(commands) == 1 else 's'}" + (f": {head}." if head else "."))
    for status, meaning in _SUMMARY_DETAILS:
        listed = [f"{label} ({record['action'] or 'command'}"
                  + (f" on tab {record['tabId']})" if "tabId" in record else ")")
                  for label, record in commands if record["status"] == status]
        if listed:
            more = len(listed) - _SUMMARY_LIST_MAX
            parts.append(f"{meaning}: {', '.join(listed[:_SUMMARY_LIST_MAX])}"
                         + (f", and {more} more." if more > 0 else "."))
    return " ".join(parts)


async def _within_deadline(coro, seconds):
    """Run a container; True if its deadline cancelled it, so no later step starts."""
    try:
        await asyncio.wait_for(coro, seconds)
        return False
    except asyncio.TimeoutError:
        return True


def _container_timeout_error(name, seconds):
    return _action_error(
        f"{name} stopped at its {seconds:g}s deadline", "container_timeout", retryable=False,
        hint=("Steps in progress have status unknown and may have taken effect; later steps were "
              "skipped. Check the page before resuming from the first step that is not ok."),
        deadlineSeconds=seconds)


# ── Checkpoint and resume (docs/checkpoint-resume.md) ──
# A batch, parallel or workflow call that did not finish ok keeps a checkpoint in bridge memory only:
# its commands (typed values included), results and step records. browser_resume needs the call's
# resume_token, which is stored as a hash and never logged or audited.
RESUME_MAX_CHECKPOINTS = 20
RESUME_TTL_SECONDS = 30 * 60
_checkpoints = {}  # call id -> checkpoint, oldest first
_checkpoint_lock = Lock()  # HTTP handler threads save and claim checkpoints
_checkpoint_clock = time.monotonic
_RESUME_KEPT = ("kept", "applied")
# A resumed step that ran ok is never sent again if it or a nested step may change the page or
# its data, and after an unknown outcome only when the caller observed it did not take effect.
_RESUME_WRITE_ACTIONS = (_REPEAT_UNSAFE_ACTIONS | _ASK_JS_ACTIONS | _ASK_COOKIE_ACTIONS | set(_ASK_DATA_ACTIONS)
                         | {"broadcast"})
_TEMPLATE_INDEX = _re.compile(r"\$\{\$(\d+)[.}]")


def _resume_token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _drop_checkpoint(call):
    """Forget a checkpoint and the command parameters and results it holds."""
    _checkpoints.pop(call).clear()


def _drop_expired_checkpoints():
    now_s = _checkpoint_clock()
    for call in [c for c, checkpoint in _checkpoints.items() if checkpoint["expires"] <= now_s]:
        _drop_checkpoint(call)


def _drop_all_checkpoints():
    with _checkpoint_lock:
        for call in list(_checkpoints):
            _drop_checkpoint(call)


def _save_checkpoint(call, **checkpoint):
    """Keep a container call that did not finish ok for browser_resume; return what its response adds."""
    token = secrets.token_urlsafe(24)
    with _checkpoint_lock:
        _drop_expired_checkpoints()
        if call in _checkpoints:
            _drop_checkpoint(call)
        _checkpoints[call] = {**checkpoint, "call": call, "token": _resume_token_digest(token),
                              "expires": _checkpoint_clock() + RESUME_TTL_SECONDS}
        while len(_checkpoints) > RESUME_MAX_CHECKPOINTS:
            _drop_checkpoint(next(iter(_checkpoints)))
    return {"call": call, "resume_token": token}


def _workflow_digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


class _ResumeStep:
    """What a resume does with one step of a checkpoint (its decision):

    kept      it ran ok; not sent again, its result is reused
    applied   the caller observed that it took effect; not sent, reported ok
    run       it is sent (again)
    resume    a sequence or parallel that partly ran; its own steps are planned (children)
    refused   a step of a resumed parallel sequence whose tab was closed or claimed (error)
    """

    def __init__(self, key, cmd, record, frame):
        self.key, self.cmd, self.record, self.frame = key, cmd, record, frame
        self.decision, self.result, self.children, self.error = "run", None, None, None


class _ResumePlan(dict):
    """(sequence, index) -> _ResumeStep for the steps of one resumed container (sequence is None
    outside a parallel). A parallel's plan carries the tab claims its sequences made (owners)."""
    owners = None


def _effective_action(cmd):
    action = cmd.get("action") if isinstance(cmd, dict) else None
    return BATCH_ACTION_MAP.get(action, action) if isinstance(action, str) else action


def _resume_writes(cmd):
    """Whether cmd or a command nested in it may change the page or its data."""
    return any(_effective_action(step) in _RESUME_WRITE_ACTIONS for step in _nested_commands(cmd))


def _resume_committed(record):
    """Whether a write in this step, or nested in it, ran ok, so sending the step again repeats it."""
    action = _effective_action(record)
    if record["status"] == "ok" and action in _RESUME_WRITE_ACTIONS:
        return True
    result = record.result
    if action in _CONDITION_ACTIONS and isinstance(result, dict) and (
            "branch" in result or (isinstance(result.get("iterations"), int) and result["iterations"] > 0)):
        return True  # its condition ran
    return any(_resume_committed(n) for n in record.nested)


def _resume_uncertain(record):
    """Whether this unknown step may have changed the page: an unknown write, nested or its own."""
    if record["status"] != "unknown":
        return False
    action = _effective_action(record)
    return action in _RESUME_WRITE_ACTIONS or any(_resume_uncertain(n) for n in record.nested)


def _resumable_into(cmd, record):
    """Whether a sequence or parallel that started is resumed step by step rather than as a whole."""
    if not record.nested or not isinstance(cmd, dict):
        return False
    if cmd.get("action") == "sequence":
        return isinstance(cmd.get("do"), list)
    sequences = cmd.get("sequences")
    return (cmd.get("action") == "parallel" and isinstance(sequences, list)
            and all(isinstance(seq, list) for seq in sequences)
            and [len(seq) for seq in record.get("sequences", [])] == [len(seq) for seq in sequences])


def _kept_result(result):
    return {k: v for k, v in result.items() if k != "_stopped"} if isinstance(result, dict) else result


def _plan_steps(items, observed, problems, steps):
    """Plan one resumed command list from its last records; items are (position, key, cmd, record, frame).

    Pops the observed entries it uses; problems collects missing and invalid ones, steps every key.
    """
    plan = _ResumePlan()
    for position, key, cmd, record, frame in items:
        step = plan[position] = steps[key] = _ResumeStep(key, cmd, record, frame)
        status = record["status"]
        if status == "ok":
            step.decision, step.result = "kept", _kept_result(record.result)
        elif status == "skipped":
            pass
        elif _resumable_into(cmd, record):
            step.decision = "resume"
            step.children = _plan_children(cmd, record, key, observed, problems, steps)
        else:
            # Re-sending would repeat a write that ran ok, so only "applied" can move past it.
            accepts = ["applied"] if _resume_committed(record) else ["applied", "not_applied"]
            seen = observed.pop(key, None)
            if seen is None:
                if len(accepts) == 1 or _resume_uncertain(record):
                    problems["required"].append(
                        {"step": key, "action": record.get("action"),
                         **({"tabId": record["tabId"]} if "tabId" in record else {}),
                         "status": status, "accepts": accepts})
            elif seen not in accepts:
                problems["invalid"].append({"step": key, "reason": "value not accepted", "accepts": accepts})
            elif seen == "applied":
                step.decision, step.result = "applied", {"ok": True, "observed": "applied"}
    return plan


def _plan_children(cmd, record, key, observed, problems, steps):
    if cmd["action"] == "parallel":
        plan = _plan_steps(
            (((i, j), f"{key}.{i}.{j}", c, record["sequences"][i][j],
              {"action": "parallel", "sequence": i, "index": j})
             for i, seq in enumerate(cmd["sequences"]) for j, c in enumerate(seq)),
            observed, problems, steps)
        plan.owners = dict(record.owners or {})
        return plan
    ran = {n.get("index"): n for n in record.nested}
    plan = _plan_steps(
        (((None, j), f"{key}.{j}", c, ran.get(j) or _step_record(c, "skipped", index=j),
          {"action": "sequence", "index": j})
         for j, c in enumerate(cmd["do"])),
        observed, problems, steps)
    # ${$N...} templates resolve inside a sequence only: an ok read runs again when a step that
    # runs uses its result, so that step reads fresh state. A write's old result is reused.
    for j in reversed(range(len(cmd["do"]))):
        step = plan[(None, j)]
        if step.decision == "kept" and not _resume_writes(step.cmd) and any(
                later.decision in ("run", "resume") and str(j) in _TEMPLATE_INDEX.findall(json.dumps(later.cmd))
                for (_, k), later in plan.items() if k > j):
            step.decision, step.result = "run", None
    return plan


def _plan_resume(checkpoint, observed):
    """Return (plan, None), or (None, error) when observed is missing or invalid; nothing is sent."""
    observed = dict(observed)
    problems, steps = {"required": [], "invalid": []}, {}
    frame = {"action": checkpoint["kind"]}
    if checkpoint["kind"] == "workflow":
        frame["name"] = checkpoint["workflow"]
    plan = _plan_steps(
        (((None, i), str(i), c, checkpoint["progress"][i], {**frame, "index": i})
         for i, c in enumerate(checkpoint["commands"])),
        observed, problems, steps)
    for key in observed:
        step = steps.get(key) if isinstance(key, str) else None
        reason = ("no such step" if step is None
                  else "ok, never sent again" if step.record["status"] == "ok"
                  else "skipped, never started" if step.record["status"] == "skipped"
                  else f"a {step.cmd['action']} block: name the step inside it")
        problems["invalid"].append({"step": key, "reason": reason})
    if problems["invalid"]:
        return None, _action_error(
            "observed names steps that cannot take an observation", "observed_invalid", retryable=True,
            hint=('Nothing was sent. observed takes "applied" or "not_applied" for an unknown, held or failed '
                  'step, by its progress path ("2", "0.1.3" for a parallel sequence step).'),
            call=checkpoint["call"], steps=problems["invalid"])
    if problems["required"]:
        return None, _action_error(
            "Resume needs an observation for each step that may have taken effect", "observed_required",
            retryable=True,
            hint=('Nothing was sent. Check the page for each listed step, then resume with observed: '
                  '"not_applied" sends it again, "applied" marks it ok without sending it.'),
            call=checkpoint["call"], steps=problems["required"])
    return plan, None


def _claim_checkpoint(body):
    """Check a resume request and take its checkpoint out of the store while it runs.

    Returns (checkpoint, plan, None), or (None, None, (http status, error)); nothing is sent.
    """
    call = body.get("call")
    call = int(call) if isinstance(call, str) and call.isdigit() else call
    with _checkpoint_lock:
        _drop_expired_checkpoints()
        checkpoint = _checkpoints.get(call) if type(call) is int else None
        if checkpoint is None:
            return None, None, (404, _action_error(
                f"No checkpoint for call {call!r}", "checkpoint_not_found", retryable=False,
                hint=(f"It finished ok, expired ({RESUME_TTL_SECONDS // 60} minutes after the call), was one of "
                      f"more than {RESUME_MAX_CHECKPOINTS} newer checkpoints, is being resumed, stopped on an "
                      "internal error, or the bridge restarted. Check the page and continue by hand."),
                call=call))
        token = body.get("resume_token")
        if not isinstance(token, str) or not hmac.compare_digest(_resume_token_digest(token), checkpoint["token"]):
            return None, None, (403, _action_error(
                "resume_token does not match this call's checkpoint", "resume_token_invalid", retryable=False,
                hint="Nothing was sent. Pass the resume_token from the response that returned this call.",
                call=call))
        if checkpoint["kind"] == "workflow" and _workflow_digest(checkpoint["path"]) != checkpoint["digest"]:
            return None, None, (409, _action_error(
                f"Workflow '{checkpoint['workflow']}' changed since call {call}", "workflow_changed",
                retryable=False,
                hint="Nothing was sent. Its steps may differ now; check the page and run the workflow again.",
                call=call, workflow=checkpoint["workflow"]))
        observed = body.get("observed") or {}
        if not isinstance(observed, dict):
            return None, None, (400, _action_error(
                "observed must map step paths to applied or not_applied", "observed_invalid", retryable=True,
                hint="Nothing was sent.", call=call))
        plan, problem = _plan_resume(checkpoint, observed)
        if problem:
            return None, None, (400, problem)
        del _checkpoints[call]
    return checkpoint, plan, None


def _resumed_records(plan, parent):
    """Records of the steps a resumed container keeps or the caller reported applied.

    They are added when the container starts, so steps it does not reach still report ok. Each
    applied step is audited as not sent.
    """
    records = []
    for step in plan.values():
        if step.decision == "kept":
            record = step.record.copy_with(resumed="kept")
            record.result = step.result
            records.append(record)
        elif step.decision == "applied":
            params = {k: v for k, v in step.cmd.items() if k != "action"}
            token = _audit_parent.set({**parent, "path": [*parent["path"], step.frame]})
            try:
                _audit(_effective_action(step.cmd), {**params, "observed": "applied", "sent": False}, True, None, 0)
            finally:
                _audit_parent.reset(token)
            record = _StepRecord({k: v for k, v in step.record.items() if k != "sequences"},
                                 status="ok", resumed="applied")
            record.result = step.result
            records.append(record)
    return records


def _resume_pending_commands(step):
    """The commands a resumed step will send, nested ones included."""
    if step.decision == "run":
        return _nested_commands(step.cmd)
    if step.decision == "resume":
        return [c for child in step.children.values() for c in _resume_pending_commands(child)]
    return []


async def _refuse_unsafe_sequences(plan, owners, call):
    """Refuse each resumed parallel sequence whose tab was closed or is written by another sequence or call.

    The steps it would send fail with tab_closed, tab_owned or tab_claimed instead; none is sent.
    """
    pending = {}  # sequence -> (tabs its steps act on, tabs they write)
    for (sequence, _), step in sorted(plan.items()):
        acts, writes = pending.setdefault(sequence, (set(), set()))
        for cmd in _resume_pending_commands(step):
            action, tab_id = _effective_action(cmd), _explicit_tab_id(cmd.get("tabId"))
            if tab_id is not None and action not in _TABLESS_ACTIONS:
                acts.add(tab_id)
                if _claims_tab(action):
                    writes.add(tab_id)
    listed = None
    for sequence, (acts, writes) in sorted(pending.items()):
        hint = "Nothing in this sequence was sent; the other sequences resumed."
        error = None
        owned = sorted(t for t in writes if owners.get(t, sequence) != sequence)
        claimed = sorted((t, other) for other, other_writers, other_owners in _running_parallels.values()
                         if other != call for t in writes if t in other_writers or t in other_owners)
        if owned:
            error = _action_error(
                f"Sequence {sequence} was not resumed: tab {owned[0]} is written by sequence {owners[owned[0]]}",
                "tab_owned", retryable=True, hint=hint, sequence=sequence, tabId=owned[0], owner=owners[owned[0]])
        elif claimed:
            error = _action_error(
                f"Sequence {sequence} was not resumed: tab {claimed[0][0]} is written by call {claimed[0][1]}, "
                "which is in progress", "tab_claimed", retryable=True,
                hint=hint + " Resume again when that call has finished.",
                sequence=sequence, tabId=claimed[0][0], call=claimed[0][1])
        elif acts:
            if listed is None:
                listed = await send_to_extension("getTabs", {}, timeout=10)
                listed = listed if isinstance(listed, dict) else {"error": str(listed)}
            closed = sorted(acts - {t.get("id") for t in listed.get("tabs") or [] if isinstance(t, dict)})
            if listed.get("error"):
                error = {**listed, "error": f"Sequence {sequence} was not resumed: could not list tabs to check "
                                            f"them ({listed['error']})"}
            elif closed:
                error = _action_error(
                    f"Sequence {sequence} was not resumed: tab {closed[0]} was closed", "tab_closed",
                    retryable=True, hint=hint + " A closed tab is never replaced; redo its steps by hand.",
                    sequence=sequence, tabId=closed[0])
        if error is not None:
            for (s, _), step in plan.items():
                if s == sequence and step.decision in ("run", "resume"):
                    step.decision, step.error = "refused", error


async def _run_step(cmd, call, **frame):
    """Run a container's nested command; its audit entries carry the call id and the step's path.

    The step also adds a record to the enclosing container's progress (_container_steps). In a
    resumed container, a kept or applied step is not sent: its result comes from the plan.
    """
    plan = _resume_plan.get()
    resume = plan.get((frame.get("sequence"), frame.get("index"))) if plan is not None else None
    if resume is not None and resume.decision in _RESUME_KEPT:
        return resume.result  # _resumed_records added its record
    token = _audit_parent.set({**call, "path": [*call["path"], frame]})
    step = _step_record(cmd, **{k: v for k, v in frame.items() if k not in ("action", "name")})
    steps = _container_steps.get()
    if steps is not None:
        steps.append(step)
    nested = []
    steps_token = _container_steps.set(nested)
    plan_token = _resume_plan.set(resume.children if resume is not None else None)
    step_token = _running_step.set(step)
    result = _UNFINISHED
    try:
        if resume is not None and resume.decision == "refused":
            nested.extend(resume.record.nested)  # keep what it ran before for a later resume
            result = resume.error
        else:
            if resume is not None and resume.decision == "resume":
                nested.extend(_resumed_records(resume.children, _audit_parent.get()))
            result = await run_command(cmd)
        return result
    finally:
        _running_step.reset(step_token)
        _resume_plan.reset(plan_token)
        _container_steps.reset(steps_token)
        _audit_parent.reset(token)
        _finish_step(step, cmd, result, nested)


async def _run_batch(commands, stop_on_warning, call, results):
    """Run a batch's commands in order, appending their results as they finish."""
    plan = _resume_plan.get()
    for i, cmd in enumerate(commands):
        r = await _run_step(cmd, call, action='batch', index=i)
        results.append(r)
        # Later commands may depend on a held one, so never run past it.
        if _is_held(r):
            r['_stopped'] = True
            break
        # Stop early if a command returned a warning/redirect and stopOnWarning is set; a resumed
        # batch does not stop again at a command it kept.
        kept = plan is not None and plan[(None, i)].decision in _RESUME_KEPT
        if stop_on_warning and not kept and isinstance(r, dict) and (r.get('warning') or r.get('error')):
            r['_stopped'] = True
            break


async def _run_workflow(name, commands, call, results):
    """Run a workflow's commands until one fails, appending their results as they finish."""
    for i, cmd in enumerate(commands):
        r = await _run_step(cmd, call, action='workflow', name=name, index=i)
        results.append(r)
        if isinstance(r, dict) and r.get('error'):
            break


async def run_command(cmd):
    """Execute a single batch command asynchronously.

    Inside a container step (_run_step) the command is audited like a direct call.
    """
    action = cmd.get('action', '')
    # Only a confirmed direct close approves closing a user tab (_take_user_tab_approval).
    params = {k: v for k, v in cmd.items() if k not in ('action', 'userTabApproved')}
    try:
        # Normalize find params: extension expects "query", users may send "selector"
        if action == 'find' and 'query' not in params and 'selector' in params:
            params['query'] = params.pop('selector')

        effective_action = BATCH_ACTION_MAP.get(action, action)
        caps_err = caps_refusal(_client_caps.get(), effective_action, ACTION_GROUPS.get(effective_action))
        if caps_err:
            return caps_err
        tab_err = _check_step_tab(effective_action, params)
        if tab_err:
            return tab_err
        policy_err = _check_policy(effective_action, params)
        if policy_err:
            return policy_err
        held = await _ask_before_act(effective_action, params, direct=False)
        if held:
            return held
        _invalidate_cache_for_action(effective_action)
    except Exception as e:
        return {'error': str(e)}
    if _audit_parent.get() is None:
        return await _execute_command(action, effective_action, params)
    audited = dict(params)
    t0 = time.time()
    try:
        result = await _execute_command(action, effective_action, params)
    except asyncio.CancelledError:
        _audit(effective_action, audited, False, 'cancelled', (time.time() - t0) * 1000)
        raise
    error = result.get('error') if isinstance(result, dict) else None
    ok = error is None and not (isinstance(result, dict) and result.get('ok') is False)
    _audit(effective_action, audited, ok, error, (time.time() - t0) * 1000)
    return result


def _condition_true(check):
    """Whether an if/while condition's executeJS reply is truthy."""
    return bool(check.get('result')) and check.get('result') not in ('false', '0', 'null', 'undefined', '')


def _dispatch_timeout(action, params, base=30):
    """WS timeout for an action: a wait's own timeout plus 5 s, else base plus any actionability wait."""
    if action in ('waitForUrl', 'waitForTitle', 'waitForElement'):
        return params.get('timeout', 10000) / 1000 + 5
    if action == 'waitForResult' or (action == 'newTab' and params.get('wait')):
        return params.get('timeout', 15000) / 1000 + 5
    return base + actionability_wait_seconds(action, params)


async def _execute_command(action, effective_action, params):
    """Dispatch a batch command that passed policy and ask-before-act checks."""
    try:
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
            if effective_action in ('waitForUrl', 'waitForTitle') or (
                    effective_action == 'newTab' and params.get('wait')):
                return await send_to_extension(effective_action, params,
                                               timeout=_dispatch_timeout(effective_action, params))
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
            return await _poll_page_wait(action, params)
        elif action == 'waitForResult':
            return await send_to_extension(action, params, timeout=_dispatch_timeout(action, params))
        elif action == 'trustedClick':
            return await trusted_click(params)
        elif action == 'sleep':
            await asyncio.sleep(params.get('ms', 1000) / 1000)
            return {'ok': True}
        elif action == 'parallel':
            sequences = params.get('sequences', [])
            writers = {}
            problem = _parallel_tab_problem(sequences, writers)
            if problem:
                return problem
            call = _audit_call()
            # A resumed parallel keeps the tab claims its sequences made before.
            plan = _resume_plan.get()
            owners = dict(plan.owners or {}) if plan is not None else {}
            if plan is not None:
                await _refuse_unsafe_sequences(plan, owners, call['call'])
            record = _running_step.get()
            if record is not None:
                record.owners = owners

            async def run_sequence(i, seq):
                _tab_owners.set((*_tab_owners.get(), (owners, i)))
                results = []
                for j, c in enumerate(seq):
                    results.append(await _run_step(c, call, action='parallel', sequence=i, index=j))
                    if _is_held(results[-1]):
                        break  # later steps may depend on the held one
                return results
            _running_parallels[id(owners)] = (call['call'], writers, owners)
            try:
                return list(await asyncio.gather(*[run_sequence(i, s) for i, s in enumerate(sequences)]))
            finally:
                del _running_parallels[id(owners)]
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
            call = _audit_call()
            last = None
            for i in range(attempts):
                last = await _run_step(inner, call, action='retry', attempt=i + 1)
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
            if check.get('error'):
                return check  # a condition that could not run is not false: run neither branch
            truthy = _condition_true(check)
            branch = then_cmds if truthy else else_cmds
            call = _audit_call()
            out = []
            for j, c in enumerate(branch):
                out.append(await _run_step(c, call, action='if', branch='then' if truthy else 'else', index=j))
            return {'branch': 'then' if truthy else 'else', 'condition': check.get('result'), 'results': out}
        elif action == 'while':
            cond = params.get('condition')
            tab_id = params.get('tabId')
            body = params.get('do', [])
            max_iters = int(params.get('maxIterations', 20))
            call = _audit_call()
            results = []
            for i in range(max_iters):
                check = await send_to_extension('executeJS', {'tabId': tab_id, 'code': cond or '0'}, timeout=10)
                if check.get('error'):
                    return {**check, 'iterations': i, 'results': results, 'stoppedBy': 'conditionError'}
                if not _condition_true(check):
                    return {'iterations': i, 'results': results, 'stoppedBy': 'condition'}
                step = []
                for j, c in enumerate(body):
                    step.append(await _run_step(c, call, action='while', iteration=i, index=j))
                results.append(step)
            return {'iterations': max_iters, 'results': results, 'stoppedBy': 'maxIterations'}
        elif action == 'try':
            body = params.get('do', [])
            catch = params.get('catch', [])
            call = _audit_call()
            try:
                results = []
                for j, c in enumerate(body):
                    r = await _run_step(c, call, action='try', block='do', index=j)
                    results.append(r)
                    if isinstance(r, dict) and r.get('error'):
                        raise RuntimeError(r['error'])
                return {'ok': True, 'results': results}
            except Exception as e:
                caught = []
                for j, c in enumerate(catch):
                    caught.append(await _run_step(c, call, action='try', block='catch', index=j))
                return {'ok': False, 'error': str(e), 'catch_results': caught}
        elif action == 'sequence':
            # Run a list of commands with variable substitution between them
            inner = params.get('do', [])
            call = _audit_call()
            results = []
            for j, command in enumerate(inner):
                resolved = _substitute_vars(command, results)
                results.append(await _run_step(resolved, call, action='sequence', index=j))
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

    def _dispatch(self, routes):
        path = self.canonical_path()
        try:
            mode = task_mode(self.headers.get(MODE_HEADER))
        except ValueError as exc:
            self.send_json(400, {"error": str(exc), "code": "invalid_mode", "retryable": False})
            return
        try:
            caps = header_caps(self.headers.get(CAPS_HEADER))
        except ValueError as exc:
            self.send_json(400, {"error": str(exc), "code": "invalid_caps", "retryable": False})
            return
        token = _task_mode.set(mode)
        caps_token = _client_caps.set(caps)
        protect = _protect_user_tabs.set(self.headers.get(PROTECT_USER_TABS_HEADER) == "1")
        called = self.headers.get(GATEWAY_TOOL_HEADER) or ""
        gateway = _gateway_tool.set(called if _TOOL_NAME.fullmatch(called) else None)
        try:
            if not permits_route(mode, path):
                self.send_json(403, _mode_error(path))
                return
            refused = None if path in OPEN_ROUTES else caps_refusal(caps, path, ROUTE_GROUPS.get(path))
            if refused and path in routes:
                self.send_json(403, refused)
                return
            handler = routes.get(path)
            if handler:
                handler()
            else:
                self.send_json(404, {"error": "Not found"})
        finally:
            _gateway_tool.reset(gateway)
            _protect_user_tabs.reset(protect)
            _client_caps.reset(caps_token)
            _task_mode.reset(token)

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
        
        self._dispatch(routes)
    
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
            "/api/resume": self.handle_resume,
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
            "/api/back": lambda: self._with_user_tab_approval("goBack", timeout=15),
            "/api/forward": lambda: self._with_user_tab_approval("goForward", timeout=15),
            "/api/get-zoom": lambda: self._forward("getZoom"),
            "/api/set-zoom": lambda: self._forward("setZoom"),
            "/api/windows": lambda: self._forward("getWindows"),
            "/api/create-window": lambda: self._forward("createWindow"),
            "/api/close-window": lambda: self._with_user_tab_approval("closeWindow", timeout=15),
            "/api/focus-window": lambda: self._forward("focusWindow"),
            "/api/resize-window": lambda: self._forward("resizeWindow"),
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
            # History and bookmark lookup (#140)
            "/api/history-search": lambda: self.handle_history_lookup("historySearch"),
            "/api/history-visits": lambda: self.handle_history_lookup("historyVisits"),
            "/api/bookmarks-search": lambda: self.handle_history_lookup("bookmarksSearch"),
        }

        self._dispatch(routes)
    
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
            "keep_alive_tabs": _active_keep_alive_tabs(),
            "diagnosis": _connection_diagnosis(),
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

    def _dom_scope(self, keys):
        """The POST body's non-null keys among keys; None after refusing an invalid maxDepth."""
        body = self.read_body() if self.command == 'POST' else {}
        max_depth = body.get('maxDepth')
        if max_depth is not None and (type(max_depth) is not int or max_depth < 0):
            self.send_json(200, {"error": "max_depth must be a non-negative integer"})
            return None
        return {key: body[key] for key in keys if body.get(key) is not None}

    def handle_dom(self):
        params = self._dom_scope(('tabId', 'selector', 'ref', 'maxDepth'))
        if params is None:
            return
        params["depth"] = params.pop("maxDepth", 6)
        result = self.run_async(send_to_extension("getAccessibilityTree", params))
        self.send_json(200, result)

    def handle_snapshot(self):
        params = self._dom_scope(('tabId', 'selector', 'ref', 'maxDepth', 'text', 'boxes', 'maxChars'))
        if params is None:
            return
        max_chars = params.get('maxChars')
        if max_chars is not None and (isinstance(max_chars, bool) or not isinstance(max_chars, (int, float))
                                      or not math.isfinite(max_chars) or max_chars < 0):
            self.send_json(200, {"error": "maxChars must be a finite non-negative number"})
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
        self.send_json(200, {"workflows": _workflow_names(), "directory": WORKFLOW_DIR})

    # ── POST Handlers ──

    def _held(self, action, body):
        """Apply ask-before-act to a direct call; send the held error and return True if held."""
        if not _ask_enabled():
            body.pop("confirmToken", None)
            return False
        held = self.run_async(_ask_before_act(action, body, direct=True))
        if held:
            self.send_json(403, held)
        return bool(held)

    def _admitted(self, action, body):
        """Apply policy and ask-before-act to a direct write; send the refusal and return False if refused."""
        policy_err = _check_policy(action, body)
        if policy_err:
            self.send_json(403, policy_err)
            return False
        if self._held(action, body):
            return False
        _invalidate_cache_for_action(action)
        return True

    def _run_admitted(self, action, body, coro, timeout=35):
        """Run an admitted direct call's coroutine, audit it, and send its result."""
        t0 = time.time()
        try:
            result = self.run_async(coro, timeout=timeout)
        except concurrent.futures.TimeoutError:
            result = _timeout_error(action, timeout)
        except Exception as e:
            result = {"error": str(e)}
        error = result.get("error") if isinstance(result, dict) else None
        _audit(action, body, isinstance(result, dict) and not error, error, (time.time() - t0) * 1000)
        self.send_json(200, result)

    def _send_extension_action(self, action, body, timeout=35, on_result=None):
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
            timeout = _dispatch_timeout(action, body, timeout)
            if spec is None:
                result = self.run_async(send_to_extension(action, body, timeout=timeout), timeout=timeout + 5)
            else:
                params = {k: v for k, v in body.items() if k != 'expect'}
                result = self.run_async(_send_with_expectation(action, params, spec, timeout=timeout,
                                                               remember=dict(body)),
                                        timeout=timeout + _outcome_budget_seconds(spec))
        except Exception as e:
            result = {"error": str(e)}
        if on_result is not None:
            result = on_result(result)
        _audit(
            action,
            body,
            isinstance(result, dict) and not result.get("error"),
            result.get("error") if isinstance(result, dict) else None,
            (time.time() - t0) * 1000,
        )
        self.send_json(200, result)
    
    def handle_upload_file(self):
        body = self.read_body()
        if not self._admitted("uploadFile", body):
            return
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
        if self._admitted("trustedClick", body):
            self._run_admitted("trustedClick", body, trusted_click(body))
    
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
        self._with_user_tab_approval("navigate", body=_wrap_image_url(self.read_body()))

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
            if self._admitted("executeJS", body):
                self._run_admitted("executeJS", body, _js_full_async(body), timeout=float(body.get("timeout", 180)))
            return
        self._send_extension_action("executeJS", body)
    
    def handle_highlight(self):
        self._send_extension_action("highlight", self.read_body())

    def handle_clear_highlight(self):
        self._send_extension_action("clearHighlight", {})

    def handle_close_tab(self):
        # With force, the extension disarms beforeunload once it has agreed to close the tab.
        self._with_user_tab_approval("closeTab")

    def _with_user_tab_approval(self, action, timeout=35, body=None):
        body = self.read_body() if body is None else body
        body.pop("userTabApproved", None)
        rejected = _take_user_tab_approval(action, body)
        self._send_extension_action(action, body, timeout=timeout,
                                    on_result=lambda result: _offer_user_tab_approval(action, body, result, rejected))

    def handle_switch_tab(self):
        self._send_extension_action("switchTab", self.read_body())

    def handle_new_tab(self):
        self._send_extension_action("newTab", _wrap_image_url(self.read_body()))

    def handle_wait_for_element(self):
        body = self.read_body()
        result = self.run_async(_poll_page_wait("waitForElement", body))
        self.send_json(200, result)

    def handle_wait_for_text(self):
        body = self.read_body()
        timeout_ms = body.get("timeout", 10000)
        if not isinstance(timeout_ms, (int, float)) or timeout_ms < 0:
            self.send_json(400, {"error": "timeout must be non-negative milliseconds"})
            return
        timeout_s = timeout_ms / 1000 + 5
        result = self.run_async(_poll_page_wait("waitForText", body), timeout=timeout_s + 1)
        self.send_json(200, result)

    def handle_wait_for_result(self):
        self._send_extension_action("waitForResult", self.read_body())

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
            "active_total": len(_active_keep_alive_tabs()),
        })

    def handle_keep_alive_stop(self):
        body = self.read_body()
        tab_ids = body.get("tabIds")  # None means stop all
        stopped = self.run_async(_stop_keep_alive_async(tab_ids))
        self.send_json(200, {
            "ok": True,
            "stopped": stopped,
            "active_total": len(_active_keep_alive_tabs()),
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
        if self._admitted("syncBarrier", body):
            self._run_admitted("syncBarrier", body, _sync_barrier_async(
                body.get("tabIds", []),
                body.get("predicate", "1"),
                timeout=body.get("timeout", 30000),
                poll_interval=body.get("pollInterval", 300),
            ))

    def handle_retry(self):
        body = self.read_body()
        default_tab, refused = _container_default_tab(body)
        if refused:
            self.send_json(*refused)
            return
        cmd = {
            "action": "retry",
            **body,
        }

        async def run_retry():
            _default_tab.set(default_tab)
            return await run_command(cmd)

        # Every attempt may wait for its target, and the backoff sleeps add up.
        result = self.run_async(run_retry(), timeout=max(35, command_wait_seconds(cmd) + 15))
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
        if self._admitted("saveSession", body):
            self._run_admitted("saveSession", body,
                               _save_session_async(body.get("name") or "default", body.get("urls") or []))

    def handle_load_session(self):
        body = self.read_body()
        if self._admitted("loadSession", body):
            self._run_admitted("loadSession", body, _load_session_async(body.get("name") or "default"))

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
        self.send_json(200, {
            "bridge_version": BRIDGE_VERSION,
            "api_version": API_VERSION,
            "extension_connected": extension_ws is not None,
            "extension_version": extension_info.get("version"),
            "extension_build": extension_info.get("build"),
            "extension_generation": extension_ws_generation,
            "ports": {"http": HTTP_PORT, "ws": WS_PORT},
            "keep_alive_tabs": _active_keep_alive_tabs(),
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
                                              f"{ASK_BEFORE_ACT_ENV}=1, --ask-before-act, or {ASK_LOCK_FILE}): "
                                              + "; ".join(loosened),
                                     "code": "policy_locked", "policy": dict(_policy)})
                return
        try:
            history, loosened = _history_policy_update(body)
        except ValueError as exc:
            self.send_json(400, {"error": f"Invalid policy: {exc}", "code": "invalid_policy",
                                 "policy": dict(_policy)})
            return
        if loosened:
            self.send_json(403, {"error": "The history scope can only be tightened through the policy API; "
                                          f"loosen it with {HISTORY_ALLOW_ENV}, {HISTORY_DENY_ENV} or "
                                          f"{HISTORY_MAX_DAYS_ENV} and restart the bridge: " + "; ".join(loosened),
                                 "code": "policy_locked", "policy": dict(_policy)})
            return
        _policy.update(history)
        _raise_history_url_floor(body)
        if "ask" in body: _policy["ask"] = body["ask"]
        if "askUrls" in body: _policy["askUrls"] = list(body["askUrls"])
        if "allow" in body: _policy["allow"] = list(body["allow"])
        if "deny" in body: _policy["deny"] = list(body["deny"])
        if "readonly" in body: _policy["readonly"] = bool(body["readonly"])
        self.send_json(200, {"ok": True, "policy": dict(_policy)})

    def handle_get_policy(self):
        policy = dict(_policy)
        if _task_mode.get() != "act":
            policy.update(mode=_task_mode.get(), ask=_ask_enabled(), confirmAllowed=False)
        self.send_json(200, policy)

    def handle_history_lookup(self, action):
        self.send_json(200, self.run_async(_history_lookup(action, self.read_body()), timeout=40))

    def handle_batch(self):
        body = self.read_body()
        default_tab, refused = _container_default_tab(body)
        if refused:
            self.send_json(*refused)
            return
        commands = body.get('commands', [])
        stop_on_warning = body.get('stopOnWarning', False)
        results, steps = [], []
        call = _audit_call()

        async def run_all():
            _container_steps.set(steps)
            _default_tab.set(default_tab)
            await _run_batch(commands, stop_on_warning, call, results)

        # Commands run in sequence, so the deadline must cover every wait in the batch.
        deadline = batch_deadline_seconds(commands)
        timed_out = self.run_async(_within_deadline(run_all(), deadline), timeout=container_settle_seconds(deadline))
        response = {'results': results, **_container_progress(commands, steps)}
        if timed_out:
            response.update(_container_timeout_error('Batch', deadline))
        response['summary'] = _container_summary(response['progress'], deadline if timed_out else None)
        if response['status'] != 'ok':
            response.update(_save_checkpoint(call['call'], kind='batch', commands=commands,
                                             progress=response['progress'], stopOnWarning=stop_on_warning,
                                             tabId=default_tab))
        self.send_json(200, response)

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
            seconds = body.get("seconds", 5)
            # A non-number would make every later _cache_get raise TypeError.
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not seconds >= 0:
                self.send_json(400, {"error": "seconds must be a non-negative number"})
                return
            CACHE_TTL = seconds
            self.send_json(200, {"ok": True, "ttl": CACHE_TTL})
        else:
            self.send_json(400, {"error": f"Unknown cache action: {action}"})

    def handle_workflow(self):
        body = self.read_body()
        default_tab, refused = _container_default_tab(body)
        if refused:
            self.send_json(*refused)
            return
        name = body.get("name", "")
        variables = body.get("variables", {})

        if not name:
            self.send_json(400, {"error": "Missing 'name' parameter"})
            return

        # Sanitize filename
        safe_name = _re.sub(r'[^\w\-]', '', name)
        filepath = os.path.join(WORKFLOW_DIR, f"{safe_name}.md")

        if not os.path.isfile(filepath):
            self.send_json(404, {"error": f"Workflow '{name}' not found", "available": _workflow_names()})
            return

        # The checkpoint keeps the hash of the file the commands came from.
        with open(filepath, "rb") as f:
            content = f.read()
        # Universal newlines, as _parse_workflow reads a file.
        text = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        commands, err = _parse_workflow_text(text, variables)
        if err:
            self.send_json(400, {"error": err})
            return

        results, steps = [], []
        call = _audit_call()

        async def run_workflow():
            _container_steps.set(steps)
            _default_tab.set(default_tab)
            await _run_workflow(name, commands, call, results)

        deadline = WORKFLOW_DEADLINE_SECONDS
        timed_out = self.run_async(_within_deadline(run_workflow(), deadline), timeout=container_settle_seconds(deadline))
        response = {"workflow": name, "steps": len(commands), "results": results,
                    **_container_progress(commands, steps)}
        if timed_out:
            response.update(_container_timeout_error('Workflow', deadline))
        response["summary"] = _container_summary(response["progress"], deadline if timed_out else None)
        if response["status"] != "ok":
            response.update(_save_checkpoint(call["call"], kind="workflow", commands=commands,
                                             progress=response["progress"], workflow=name, path=filepath,
                                             digest=hashlib.sha256(content).hexdigest(), tabId=default_tab))
        self.send_json(200, response)

    def handle_resume(self):
        body = self.read_body()
        checkpoint, plan, refused = _claim_checkpoint(body)
        if refused:
            self.send_json(*refused)
            return
        call = {"call": checkpoint["call"], "path": [], "resumed": True}
        commands, workflow = checkpoint["commands"], checkpoint.get("workflow")
        results, steps = [], []

        async def resume():
            _container_steps.set(steps)
            _resume_plan.set(plan)
            # Steps that name no tab resume on the original call's default tab, not a later one.
            _default_tab.set(checkpoint.get("tabId"))
            steps.extend(_resumed_records(plan, call))
            if checkpoint["kind"] == "workflow":
                await _run_workflow(workflow, commands, call, results)
            else:
                await _run_batch(commands, checkpoint["stopOnWarning"], call, results)

        # The MCP server cannot see the stored commands, so a resume has a fixed deadline.
        deadline = RESUME_DEADLINE_SECONDS
        failure = None
        try:
            timed_out = self.run_async(_within_deadline(resume(), deadline),
                                       timeout=container_settle_seconds(deadline))
        except Exception as exc:
            timed_out, failure = False, type(exc).__name__
        response = {"workflow": workflow, "steps": len(commands)} if checkpoint["kind"] == "workflow" else {}
        response.update(results=results, **_container_progress(commands, steps))
        if timed_out:
            response.update(_container_timeout_error(f"Resumed {checkpoint['kind']}", deadline))
        if failure:
            # Fail closed: steps may have been sent, so the checkpoint is not restored.
            _log(f"Resume of call {call['call']} stopped by an internal error ({failure}); checkpoint dropped")
            response.update(_action_error(
                f"Resume stopped by an internal error ({failure}); its checkpoint was dropped", "resume_failed",
                retryable=False,
                hint=("Steps in progress may have taken effect, and this call cannot be resumed again. "
                      "Check the page, then continue by hand from progress."),
                call=call["call"]))
        response["summary"] = _container_summary(response["progress"], deadline if timed_out else None)
        if failure is None and response["status"] != "ok":
            response.update(_save_checkpoint(call["call"], **{k: v for k, v in checkpoint.items()
                                                              if k not in ("call", "token", "expires", "progress")},
                                             progress=response["progress"]))
        checkpoint.clear()
        self.send_json(200, response)

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
        _drop_all_checkpoints()
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
        help=f"start: hold sensitive actions for confirmation, locked on until {ASK_LOCK_FILE} is deleted "
             f"(also ${ASK_BEFORE_ACT_ENV}=1)",
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
    if _ask_locked_on_start(args.ask_before_act):
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
