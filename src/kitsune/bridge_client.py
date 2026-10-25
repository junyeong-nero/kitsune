"""Requests to the shared Kitsune bridge, which a BridgeClient starts on demand.

The MCP server sends every tool's bridge request through one BridgeClient. Connection failures
are returned as results in the action error contract (docs/api-reference.md#connection-diagnosis),
not raised. This module imports no MCP code.
"""

import os
import re
import subprocess
import sys
import time

import httpx

from . import bridge_control
from .bridge_control import BRIDGE, HTTP_PORT, LOG_FILE, TOKEN_FILE, WS_PORT, headers as _headers
from .capabilities import CAPS_HEADER
from .task_modes import MODE_HEADER, PROTECT_USER_TABS_HEADER
from .version import BRIDGE_BUILD

BRIDGE_START_TIMEOUT = 15
# A listening bridge answers /api/status at once. Probes during a start wait no longer than
# READY_PROBE_TIMEOUT; a started bridge whose port accepts connections but has not answered
# for NOT_RESPONDING_SECONDS hangs (a bridge that cannot listen exits within that time).
READY_PROBE_TIMEOUT = 1
NOT_RESPONDING_SECONDS = 3
# The extension retries its WebSocket at least every 15 s (MAX_RECONNECT_DELAY in background.js),
# so it reaches a bridge this client started within EXTENSION_RECONNECT_SECONDS (#258).
EXTENSION_RECONNECT_SECONDS = 16


def _bridge_matches(status: dict) -> bool:
    return status.get("build") == BRIDGE_BUILD or os.environ.get("KITSUNE_NO_BRIDGE_RESTART") == "1"


STARTUP_EXCERPT_CHARS = 300
_SECRET_PATTERN = re.compile(r"(?i)(bearer\s+|token[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+")


def _startup_log_excerpt(offset: int) -> str:
    """The last line a bridge that exited at startup wrote to LOG_FILE after offset, without secrets.

    For a Python traceback that is the exception, such as a port that is already in use.
    """
    try:
        with open(LOG_FILE, "rb") as log:
            log.seek(max(offset, log.seek(0, os.SEEK_END) - 8192))
            text = log.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    excerpt = lines[-1]
    token = _headers().get("Authorization", "").removeprefix("Bearer ")
    for secret in {token, os.environ.get("KITSUNE_TOKEN")}:
        if secret:
            excerpt = excerpt.replace(secret, "[redacted]")
    return _SECRET_PATTERN.sub(r"\1[redacted]", excerpt)[:STARTUP_EXCERPT_CHARS]


# Connection failures share the action error contract (docs/api-reference.md#connection-diagnosis).
# Nothing reached the browser, so repeating the request is safe. Never include the token.
def _bridge_unavailable(message: str, **state) -> dict:
    error = {"error": message, "code": "bridge_unavailable", "retryable": True,
             "hint": "Run `kitsune-bridge start` (`uv run kitsune-bridge start` in a checkout) in a terminal "
                     f"to see why the bridge does not start; the MCP server starts it on demand. See {LOG_FILE}"}
    if state:
        error["state"] = state
    return error


def _not_responding(pid: int) -> dict:
    error = _bridge_unavailable(f"Kitsune bridge (PID {pid}) accepts connections on port {HTTP_PORT} but does not "
                                f"answer. See {LOG_FILE}", port=HTTP_PORT, reason="not_responding", pid=pid)
    error["hint"] = ("The bridge hangs: run `kitsune-bridge stop`, then retry. If it hangs again, run "
                     f"`kitsune-bridge start` in a terminal and see {LOG_FILE}. If another program holds port "
                     f"{HTTP_PORT}, find it with `lsof -nP -iTCP:{HTTP_PORT} -sTCP:LISTEN`.")
    return error


def _bridge_disconnected(method: str, exc: httpx.TransportError) -> dict:
    """The bridge closed the connection before its reply (#235).

    Bridge handlers read the whole request body before they act, so a request whose body could not
    be sent did not run; once it was sent, a POST may have run.
    """
    sent = not isinstance(exc, httpx.WriteError)
    if not sent:
        hint = "The bridge closed the connection before it read the request, so the command did not run; repeat it."
    elif method == "GET":
        hint = "The request was a read; repeat it."
    else:
        hint = ("The command may have run before the connection closed; check the page with browser_snapshot "
                "before repeating it.")
    return {"error": str(exc) or "Kitsune bridge closed the connection before replying",
            "code": "bridge_disconnected", "retryable": not sent or method == "GET",
            "hint": f"{hint} If this repeats, see {LOG_FILE}", "state": {"requestSent": sent}}


def _token_mismatch() -> dict:
    source = "KITSUNE_TOKEN" if os.environ.get("KITSUNE_TOKEN") else TOKEN_FILE
    return {"error": f"The Kitsune bridge on port {HTTP_PORT} rejected this MCP server's token",
            "code": "token_mismatch", "retryable": True,
            "hint": "Set the same KITSUNE_TOKEN and KITSUNE_HOME (or neither) for every MCP client, or run "
                    "`kitsune-bridge stop` so the next tool call starts a bridge with this server's token.",
            "state": {"port": HTTP_PORT, "tokenSource": source}}


def _port_conflict(port: int, reason: str, message: str) -> dict:
    setting = "KITSUNE_HTTP_PORT" if port == HTTP_PORT else "KITSUNE_WS_PORT (and the extension's WS_URL)"
    owner = "answers but is not a Kitsune bridge" if reason == "not_kitsune" else "is used by another program"
    return {"error": message, "code": "port_conflict", "retryable": True,
            "hint": f"Port {port} {owner}. Find it with `lsof -nP -iTCP:{port} -sTCP:LISTEN` and stop it, "
                    f"or set {setting} to a free port for every client.",
            "state": {"port": port, "reason": reason}}


class BridgeClient:
    """Bridge requests that carry one caller's capability groups, task mode and user-tab protection.

    caps are the groups the bridge lets these requests use: it refuses routes and container steps
    of other groups (#230). Without the header nothing would be refused, so caps has no default.
    extra_headers, if set, returns headers to add to each request, such as the tool a gateway call
    runs.
    """

    def __init__(self, *, caps, mode: str = "act", protect_user_tabs: bool = False, extra_headers=None):
        self.caps = ",".join(sorted(caps))
        self.mode = mode
        self.protect_user_tabs = protect_user_tabs
        self.extra_headers = extra_headers
        self._extension_wait_until = 0.0  # set when this client starts a bridge; see await_extension

    def ensure_bridge(self) -> dict | None:
        """Start or replace the shared bridge, serializing lifecycle changes; return an error result."""
        try:
            status = bridge_control.status()
            if status is not None and _bridge_matches(status):
                return None
            with bridge_control.lifecycle_lock():
                # Another MCP server may already have replaced it while we waited.
                status = bridge_control.status()
                if status is not None:
                    if _bridge_matches(status):
                        return None
                    error = bridge_control.stop()
                    if error:
                        return _bridge_unavailable(error)
                with open(LOG_FILE, "ab") as log:
                    log_start = log.tell()
                    process = subprocess.Popen(
                        [sys.executable, "-m", "kitsune.bridge"],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        start_new_session=True,  # outlive this process
                        env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    )
                deadline = time.monotonic() + BRIDGE_START_TIMEOUT
                unanswered_since = None
                while time.monotonic() < deadline:
                    try:
                        status = bridge_control.status(timeout=READY_PROBE_TIMEOUT)
                        unanswered_since = None
                    except httpx.TimeoutException:
                        # The port accepts connections but nothing answers. If it is another program,
                        # the new bridge cannot listen and exits; if it stays alive, it hangs.
                        status = None
                        if unanswered_since is None:
                            unanswered_since = time.monotonic()
                        if process.poll() is None and time.monotonic() - unanswered_since >= NOT_RESPONDING_SECONDS:
                            return _not_responding(process.pid)
                    if status is not None and status.get("build") == BRIDGE_BUILD:
                        self._extension_wait_until = time.monotonic() + EXTENSION_RECONNECT_SECONDS
                        return None
                    if process.poll() is not None:
                        break  # it exited at startup; diagnose now instead of at the deadline
                    time.sleep(0.3)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return _token_mismatch()
            return _port_conflict(HTTP_PORT, "not_kitsune", f"Cannot start Kitsune bridge: {exc}")
        except ValueError as exc:  # a reply that is not a Kitsune status
            return _port_conflict(HTTP_PORT, "not_kitsune", f"Cannot start Kitsune bridge: {exc}")
        except httpx.TimeoutException:
            # Before a start: a running bridge that hangs, or another program on the port.
            pid = bridge_control.daemon_pid()
            if pid is not None:
                return _not_responding(pid)
            return _port_conflict(HTTP_PORT, "in_use", f"Port {HTTP_PORT} accepts connections but does not answer, "
                                                       "and no Kitsune bridge runs")
        except (httpx.HTTPError, OSError) as exc:
            return _bridge_unavailable(f"Cannot start Kitsune bridge: {exc}. See {LOG_FILE}")
        exited = {}
        if process.poll() is not None:
            exited = {"exitCode": process.returncode}
            excerpt = _startup_log_excerpt(log_start)
            if excerpt:
                exited["logExcerpt"] = excerpt
        elif unanswered_since is not None:
            return _not_responding(process.pid)
        # Nothing answers: the new bridge may have exited because it could not listen.
        for port in (HTTP_PORT, WS_PORT) if status is None else ():
            if bridge_control.port_in_use(port):
                error = _port_conflict(port, "in_use", f"Kitsune bridge did not start: port {port} is in use")
                error["state"].update(exited)
                return error
        if exited:
            reason = f": {exited['logExcerpt']}" if "logExcerpt" in exited else ""
            return _bridge_unavailable(
                f"Kitsune bridge exited at startup with code {exited['exitCode']}{reason}. See {LOG_FILE}", **exited)
        if status is None:
            # Still running, but it never listened: a slow start cannot be told apart before the deadline.
            return _bridge_unavailable(
                f"Kitsune bridge (PID {process.pid}) runs but did not listen on port {HTTP_PORT} within "
                f"{BRIDGE_START_TIMEOUT}s. See {LOG_FILE}", port=HTTP_PORT, reason="not_listening", pid=process.pid)
        return _bridge_unavailable(f"Kitsune bridge did not start within {BRIDGE_START_TIMEOUT}s. See {LOG_FILE}")

    def await_extension(self) -> None:
        """Before the first command to a bridge this client started, wait for the extension to connect.

        The new bridge answers before the extension's WebSocket reconnects, and a command sent in
        between fails with extension_disconnected (#258). Waiting instead of retrying sends each
        command once. Past the deadline, or for any other diagnosis, the command runs and returns the
        bridge's own error.
        """
        while time.monotonic() < self._extension_wait_until:
            try:
                status = bridge_control.status(timeout=READY_PROBE_TIMEOUT)
            except (httpx.HTTPError, ValueError):
                break  # the command reports why
            if status is None or (status.get("diagnosis") or {}).get("code") != "extension_disconnected":
                break
            time.sleep(0.3)
        self._extension_wait_until = 0.0

    def request(self, method: str, endpoint: str, **kwargs) -> dict:
        for attempt in range(2):
            if endpoint != "/api/status":  # browser_status reports the extension as it is
                self.await_extension()
            try:
                headers = {**_headers(), CAPS_HEADER: self.caps}
                if self.mode != "act":
                    headers[MODE_HEADER] = self.mode
                if self.protect_user_tabs:
                    headers[PROTECT_USER_TABS_HEADER] = "1"
                if self.extra_headers:
                    headers.update(self.extra_headers())
                r = httpx.request(method, f"{BRIDGE}{endpoint}", headers=headers, **kwargs)
                if r.status_code == 401:
                    return _token_mismatch()
                try:
                    return r.json()
                except ValueError:
                    # Every bridge route the server calls answers JSON on 2xx, so a 2xx page is another program.
                    if 200 <= r.status_code < 300:
                        return _port_conflict(HTTP_PORT, "not_kitsune",
                                              f"Port {HTTP_PORT} did not answer as a Kitsune bridge")
                    # Keep what the HTTP layer said instead of a JSON decoding message.
                    return {"error": f"Kitsune bridge returned HTTP {r.status_code} without a JSON body",
                            "state": {"httpStatus": r.status_code, "body": r.text[:200]}}
            except httpx.ConnectError:
                if attempt:
                    break
                error = self.ensure_bridge()
                if error:
                    return error
            except httpx.TimeoutException as e:
                # The bridge may still run the command; only GET requests are known to be reads.
                return {"error": str(e) or "Kitsune bridge request timed out", "code": "timeout",
                        "retryable": method == "GET",
                        "hint": "The command's effect is unknown; check the page with browser_snapshot "
                                "before repeating it."}
            except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError) as e:
                return _bridge_disconnected(method, e)
            except Exception as e:
                return {"error": str(e)}
        return _bridge_unavailable(f"Kitsune bridge is not reachable. See {LOG_FILE}")

    def get(self, endpoint: str) -> dict:
        """GET request to bridge."""
        return self.request("GET", endpoint, timeout=15)

    def post(self, endpoint: str, data: dict, timeout: float = 15) -> dict:
        """POST request to bridge."""
        return self.request("POST", endpoint, json=data, timeout=timeout)
