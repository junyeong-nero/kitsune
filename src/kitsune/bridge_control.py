"""Local bridge lifecycle operations shared by the bridge client and CLI."""

from contextlib import contextmanager, ExitStack
import functools
import os
from pathlib import Path
import shlex
import signal
import socket
import ssl
import subprocess
import time

import httpx

from . import file_lock, private_files

HTTP_PORT = int(os.environ.get("KITSUNE_HTTP_PORT", "47651"))
WS_PORT = int(os.environ.get("KITSUNE_WS_PORT", "47652"))
BRIDGE = f"http://127.0.0.1:{HTTP_PORT}"
DATA_DIR = os.environ.get("KITSUNE_HOME", os.path.join(os.path.expanduser("~"), ".kitsune"))
TOKEN_FILE = os.path.join(DATA_DIR, "token")
LOCK_FILE = os.path.join(DATA_DIR, "bridge.lock")
PID_FILE = os.path.join(DATA_DIR, "bridge.pid")
LOG_FILE = os.path.join(DATA_DIR, "bridge.log")
STOP_TIMEOUT = 5


@contextmanager
def shutdown_signals(loop, callback):
    """Restore both asyncio and Windows main-thread signal handlers on exit."""
    with ExitStack() as cleanup:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, callback)
            except NotImplementedError:
                previous = signal.getsignal(sig)
                signal.signal(sig, lambda _signum, _frame: loop.call_soon_threadsafe(callback))
                cleanup.callback(signal.signal, sig, previous)
            else:
                cleanup.callback(loop.remove_signal_handler, sig)
        yield


@functools.cache
def tls_context() -> ssl.SSLContext:
    """The TLS context every bridge request passes to httpx, built once.

    The bridge speaks plain HTTP, so it is never used. Without it httpx's top-level functions
    build a client per call, and with it a context that loads the CA bundle: about 10 ms of CPU
    per request, most of the MCP server's time per tool call under concurrent load (#298).
    """
    return ssl.create_default_context()


def headers() -> dict:
    """Read each time so a newly created token is picked up."""
    token = os.environ.get("KITSUNE_TOKEN")
    if not token:
        try:
            token = Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def status(timeout: float = 2) -> dict | None:
    try:
        response = httpx.get(f"{BRIDGE}/api/status", headers=headers(), timeout=timeout, verify=tls_context())
    except httpx.ConnectError:
        return None
    except httpx.ConnectTimeout:
        # A closed loopback port can time out instead of refusing immediately
        # on Windows. A bind probe distinguishes it from a hung live listener.
        if not port_in_use(HTTP_PORT):
            return None
        raise
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Bridge returned invalid status")
    return data


def port_in_use(port: int) -> bool:
    """Whether the bridge could not listen on this port; binding never contacts its owner."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


@contextmanager
def lifecycle_lock():
    """Serialize starts, automatic replacements, and CLI stops across clients."""
    private_files.ensure_directory(DATA_DIR)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as lock:
        file_lock.lock_exclusive(lock)
        yield


def try_lifecycle_lock():
    """Non-blocking lifecycle_lock for the daemon; close the returned file to release it."""
    private_files.ensure_directory(DATA_DIR)
    lock = os.fdopen(os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600), "r+")
    try:
        file_lock.lock_exclusive(lock, blocking=False)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def daemon_pid() -> int | None:
    """PID of the bridge daemon holding this KITSUNE_HOME's pid lock, if one runs."""
    return _locked_pid()


def _locked_pid() -> int | None:
    """Only trust the PID while a daemon holds the pid-file lock.

    Never unlink this file: replacing its inode would bypass the daemon lock.
    An unlocked/stale pid file must never be used to signal a recycled PID.
    """
    try:
        with open(PID_FILE, "r+", encoding="utf-8") as pid_file:
            try:
                file_lock.lock_exclusive(pid_file, blocking=False)
            except BlockingIOError:
                pid = int(pid_file.read().strip())
                return pid if pid > 1 and pid != os.getpid() else None
    except (OSError, ValueError):
        pass
    return None


def _terminate_daemon() -> bool:
    pid = _locked_pid()
    if pid is None:
        return False
    if os.name == "nt":
        return _terminate_windows_daemon(pid)
    # A second identity check avoids signaling an unrelated PID even if a
    # malformed pid file happens to be locked by another local program.
    try:
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="], capture_output=True,
            text=True, check=True, timeout=2,
        ).stdout
        args = shlex.split(command)
        is_bridge = any(args[i:i + 2] == ["-m", "kitsune.bridge"] for i in range(len(args)))
        is_bridge = is_bridge or any(Path(arg).name == "kitsune-bridge" for arg in args[:2])
        if not is_bridge or _locked_pid() != pid:
            return False
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _terminate_windows_daemon(pid: int) -> bool:
    """Use a process object that checks creation identity before termination."""
    import psutil

    try:
        process = psutil.Process(pid)
        args = process.cmdline()
        is_bridge = args[1:3] == ["-m", "kitsune.bridge"]
        is_bridge = is_bridge or any(Path(arg).name.lower() == "kitsune-bridge.exe" for arg in args[:2])
        if not is_bridge or _locked_pid() != pid:
            return False
        # psutil.terminate checks PID reuse; os.kill(SIGTERM) on Windows does
        # not, and parsing the executable name alone could target any Python.
        process.terminate()
        return True
    except (OSError, ValueError, psutil.Error):
        return False


def _wait_stopped() -> bool:
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if status() is None and _locked_pid() is None:
                return True
        except httpx.TransportError:
            # Connections can be reset while the HTTP listener is closing.
            pass
        time.sleep(0.1)
    return False


def stop() -> str | None:
    """Stop under lifecycle_lock; return an actionable error instead of pkill."""
    try:
        try:
            running = status()
        except httpx.TransportError:
            running = {}  # Unresponsive listener: still try shutdown and the verified PID.
        if running is None and _locked_pid() is None:
            return None
        if running is not None:
            try:
                response = httpx.post(f"{BRIDGE}/api/shutdown", headers=headers(), timeout=2, verify=tls_context())
                if response.status_code == 200 and _wait_stopped():
                    return None
            except httpx.HTTPError:
                pass
        if _terminate_daemon() and _wait_stopped():
            return None
    except (httpx.HTTPError, ValueError) as exc:
        return f"Cannot stop Kitsune bridge: {exc}"
    return (
        "Cannot safely stop the bridge: graceful shutdown failed and verified-PID fallback "
        f"could not stop it (PID file: {PID_FILE}). Stop the specific old bridge process manually, then retry."
    )
