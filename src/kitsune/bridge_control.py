"""Local bridge lifecycle operations shared by the MCP server and CLI."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time

import httpx

HTTP_PORT = int(os.environ.get("KITSUNE_HTTP_PORT", "47651"))
BRIDGE = f"http://127.0.0.1:{HTTP_PORT}"
DATA_DIR = os.environ.get("KITSUNE_HOME", os.path.join(os.path.expanduser("~"), ".kitsune"))
TOKEN_FILE = os.path.join(DATA_DIR, "token")
LOCK_FILE = os.path.join(DATA_DIR, "bridge.lock")
PID_FILE = os.path.join(DATA_DIR, "bridge.pid")
LOG_FILE = os.path.join(DATA_DIR, "bridge.log")
STOP_TIMEOUT = 5


def headers() -> dict:
    """Read each time so a newly created token is picked up."""
    token = os.environ.get("KITSUNE_TOKEN")
    if not token:
        try:
            token = Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def status() -> dict | None:
    try:
        response = httpx.get(f"{BRIDGE}/api/status", headers=headers(), timeout=2)
    except httpx.ConnectError:
        return None
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Bridge returned invalid status")
    return data


@contextmanager
def lifecycle_lock():
    """Serialize starts, automatic replacements, and CLI stops across clients."""
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def try_lifecycle_lock():
    """Non-blocking lifecycle_lock for the daemon; close the returned file to release it."""
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    lock = os.fdopen(os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600), "r+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def _locked_pid() -> int | None:
    """Only trust the PID while a daemon holds the pid-file lock.

    Never unlink this file: replacing its inode would bypass the daemon lock.
    An unlocked/stale pid file must never be used to signal a recycled PID.
    """
    try:
        with open(PID_FILE, encoding="utf-8") as pid_file:
            try:
                fcntl.flock(pid_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                response = httpx.post(f"{BRIDGE}/api/shutdown", headers=headers(), timeout=2)
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
