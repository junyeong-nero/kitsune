"""Shared paths and helpers for the README demo recorder (see docs/recording-the-demo.md)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("KITSUNE_DEMO_DIR", "/tmp/kitsune-demo"))
QUEUE = WORK / "q"
STATE = WORK / "state.json"
LOGS = WORK / "logs"
TAKES = WORK / "takes"
PYTHON = str(REPO / ".venv" / "bin" / "python")
# The demo bridge never uses the default ports, so the user's own bridge keeps running.
HTTP_PORT, WS_PORT = 47761, 47762

sys.path.insert(0, str(REPO / "tests"))
from test_bridge_security import _request  # noqa: E402


def load(path):
    return json.loads(Path(path).read_text())


def token():
    return (WORK / "bridge-home" / "token").read_text().strip()


def api(method, path, body=None):
    """Call the demo bridge; returns (status, decoded body)."""
    headers = {"Authorization": "Bearer " + token()}
    if body is not None:
        headers["Content-Type"] = "application/json"
    status, _, text = _request(HTTP_PORT, method, path, headers, f"127.0.0.1:{HTTP_PORT}",
                               None if body is None else json.dumps(body), timeout=30)
    return status, (json.loads(text) if text.startswith(("{", "[")) else text)


def quartz(script, *args):
    """Run a snippet that needs pyobjc Quartz in a throwaway environment; returns stdout."""
    return subprocess.run(["uv", "run", "--no-project", "--with", "pyobjc-framework-Quartz", "python", "-c",
                           script, *args], capture_output=True, text=True, check=True).stdout


def windows():
    """On-screen normal windows as dicts (pid, owner, x, y, w, h); needs no capture permission."""
    out = quartz("""
import json, Quartz
for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
    if w.get("kCGWindowLayer") == 0:
        b = w["kCGWindowBounds"]
        print(json.dumps({"pid": w["kCGWindowOwnerPID"], "owner": w["kCGWindowOwnerName"],
                          "x": b["X"], "y": b["Y"], "w": b["Width"], "h": b["Height"]}))
""")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def run_in_runner(name, script, timeout=60):
    """Run a bash script through runner.py, whose Ghostty instance may capture the screen.

    Returns (exit code or None while still running, output so far).
    """
    QUEUE.mkdir(parents=True, exist_ok=True)
    for suffix in (".out", ".done", ".pid", ".running"):
        (QUEUE / f"{name}{suffix}").unlink(missing_ok=True)
    (QUEUE / f"{name}.tmp").write_text(script)
    (QUEUE / f"{name}.tmp").rename(QUEUE / f"{name}.sh")
    deadline = time.time() + timeout
    while time.time() < deadline and not (QUEUE / f"{name}.done").exists():
        time.sleep(0.1)
    done = QUEUE / f"{name}.done"
    out = QUEUE / f"{name}.out"
    return (int(done.read_text()) if done.exists() else None), (out.read_text() if out.exists() else "")
