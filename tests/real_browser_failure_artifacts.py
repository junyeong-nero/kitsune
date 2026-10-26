"""Opt-in failure artifacts for the real-browser suites (issue #293); diagnostics only.

Set KITSUNE_E2E_ARTIFACTS to a directory to keep evidence when a real-browser test or a
test class's setUpClass fails. Unset, nothing here runs: fixture handlers, bridge requests
and the browser are left exactly as they are.

When set:
- Every harness FixtureServer records, per request, when the connection was accepted
  (connection-accepted), when the request line and headers were read (request-received: method,
  path without its query) and how the handler ended, both on the server side only:
  request-handled when the handler returned and the server flushed its write buffer, or
  request-failed with the exception's type name (no message) when the handler raised, e.g.
  BrokenPipeError after the browser dropped the connection; the exception still propagates to
  the server's handle_error. status is the code the handler passed to send_response, if any,
  so request-failed can carry a 200 whose reply never fully left. Neither event says that
  the browser received the reply. Never headers or bodies.
- Every RealBrowserTestCase.call records when a bridge request was sent and answered: its path,
  HTTP status, and only the url (see sanitize_url), tabId, wait and timeout fields of the
  request and the ok, code, retryable, timedOut, status, tabId and url fields of the reply.
  open_tab (/api/new-tab, the request that timed out in #293) goes through call(). Suites that
  send bridge requests with harness._request directly (test_real_browser.py,
  test_real_browser_protected_tabs.py, test_real_browser_submit_form.py,
  test_real_browser_connection_diagnostics.py) bypass call(), so those requests are not in
  the timeline; the bridge log still lists them.
- The owned browser records top-level navigation events in its parent process: state changes
  (start/stop flags and status), location changes and tab open/close, each with Date.now().
- Resource samples hold the load average, the CPU count, and ps columns (pid, ppid, pgid, %cpu,
  %mem, rss, elapsed time, state, executable name) for the test runner, the owned bridge and the
  owned browser's process group only. They are taken when the browser starts and at each failure.
- The owned bridge runs with PYTHONUNBUFFERED=1, so its log is current when a test fails.

Retention: a passing class writes nothing. Each failure writes one new directory
  <root>/kitsune-e2e-<UTC time>-<pid>/<module.Class>/<NN>-<test or setUpClass>/
holding summary.json, timeline.jsonl (all events above on one wall clock, in time order),
resources.json, and the last MAX_LOG_BYTES of browser.log and bridge.log. A class keeps its
first MAX_PRESERVED_FAILURES_PER_CLASS failures; later ones are only counted, in a stderr note
when the class finishes. The harness never deletes or prunes anything under <root>; the
class's temporary directory is still deleted on every path, after the artifacts are copied.
Harness and fixture events share one process-wide buffer of MAX_EVENTS; when it is full the
oldest event is evicted. summary.json reports evictions of events recorded since the class
started (droppedSinceClassStart, so complete is false when the timeline lost any of them)
and, separately labelled, every eviction in the process (droppedInProcess), which includes
earlier classes' events.
Text written there has the bridge token, Authorization values, Bearer credentials and token=
query values replaced by [redacted]; URLs keep only a loopback origin and path.
"""
import collections
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import weakref

ARTIFACTS_ENV = "KITSUNE_E2E_ARTIFACTS"
MAX_EVENTS = 4000
MAX_BROWSER_EVENTS = 2000
MAX_LOG_BYTES = 256 * 1024
MAX_PRESERVED_FAILURES_PER_CLASS = 5
MAX_PATH_CHARS = 200
MAX_OWNED_PROCESSES = 64
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
REQUEST_FIELDS = ("tabId", "wait", "timeout")
REPLY_FIELDS = ("ok", "code", "retryable", "timedOut", "status", "tabId")

# Process-wide: fixture servers can start before their test class does, so harness and fixture
# events go to one bounded log and each class reads the events since it started.
_events = collections.deque(maxlen=MAX_EVENTS)
_events_lock = threading.Lock()
_dropped_in_process = 0
# FailureArtifacts of classes still running; each counts the evicted events it would have shown.
_watchers = weakref.WeakSet()
_ids = iter(range(1, sys.maxsize))
_run_directories = {}


def enabled(environ=os.environ):
    return bool(environ.get(ARTIFACTS_ENV))


def now_ms():
    return round(time.time() * 1000, 3)


def record(source, kind, **fields):
    global _dropped_in_process
    event = {"t": now_ms(), "source": source, "kind": kind, **fields}
    with _events_lock:
        if len(_events) == _events.maxlen:
            evicted = _events[0]
            _dropped_in_process += 1
            for watcher in _watchers:
                if evicted["t"] >= watcher.started:
                    watcher.dropped_events += 1
        _events.append(event)
    return event


def events_since(t):
    with _events_lock:
        return [event for event in _events if event["t"] >= t]


def next_id():
    with _events_lock:
        return next(_ids)


def sanitize_path(path):
    """A request path without its query or fragment, at most MAX_PATH_CHARS long."""
    if not isinstance(path, str):
        return None
    return re.split(r"[?#]", path, maxsplit=1)[0][:MAX_PATH_CHARS]


def sanitize_url(url):
    """Keep a loopback http(s)/ws(s) origin and path or an about: page; only the scheme otherwise."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url[:4096])
        host = parts.hostname
    except ValueError:
        return "[unparseable]"
    scheme = parts.scheme.lower()
    if scheme in ("http", "https", "ws", "wss"):
        if host not in LOOPBACK_HOSTS:
            return f"{scheme}://[non-loopback]"
        return f"{scheme}://{parts.netloc.rsplit('@', 1)[-1]}{sanitize_path(parts.path)}"
    if scheme == "about":
        return "about:" + sanitize_path(parts.path)
    return f"{scheme}:[omitted]" if scheme else "[omitted]"


_REDACTIONS = (
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)[^\r\n\"']*"), r"\1[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(token=)[^&\s\"']+"), r"\1[redacted]"),
)


def redact(text, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def recorded_handler(handler_class):
    """A subclass of handler_class that records each request's receive and finish times."""

    class Recorded(handler_class):
        def setup(self):
            super().setup()
            record("fixture", "connection-accepted", port=self.server.server_address[1])

        def handle_one_request(self):
            self._artifact_request = None
            self._artifact_status = None
            try:
                super().handle_one_request()
            except BaseException as error:
                if self._artifact_request is not None:
                    record("fixture", "request-failed", request=self._artifact_request,
                           port=self.server.server_address[1], status=self._artifact_status,
                           error=type(error).__name__)
                raise
            if self._artifact_request is not None:
                record("fixture", "request-handled", request=self._artifact_request,
                       port=self.server.server_address[1], status=self._artifact_status)

        def parse_request(self):
            parsed = super().parse_request()
            if self.command:
                self._artifact_request = next_id()
                record("fixture", "request-received", request=self._artifact_request,
                       port=self.server.server_address[1], method=self.command, path=sanitize_path(self.path))
            return parsed

        def log_request(self, code="-", size="-"):
            self._artifact_status = int(code) if isinstance(code, int) else str(code)
            super().log_request(code, size)

    Recorded.__name__ = handler_class.__name__
    Recorded.__qualname__ = handler_class.__qualname__
    return Recorded


def _fields(source, names):
    picked = {name: source[name] for name in names if name in source}
    if "url" in source:
        picked["url"] = sanitize_url(source["url"])
    return picked


def bridge_request_sent(path, body):
    request = next_id()
    record("harness", "bridge-request-sent", request=request, path=sanitize_path(path),
           **_fields(body if isinstance(body, dict) else {}, REQUEST_FIELDS))
    return request


def bridge_request_answered(request, path, status, text):
    try:
        reply = json.loads(text)
    except ValueError:
        reply = None
    record("harness", "bridge-request-answered", request=request, path=sanitize_path(path), httpStatus=status,
           **_fields(reply if isinstance(reply, dict) else {}, REPLY_FIELDS))


def bridge_request_failed(request, path, error):
    record("harness", "bridge-request-failed", request=request, path=sanitize_path(path), error=type(error).__name__)


# Parent-process scripts for RemoteDebugger.evaluate_in_parent. The listener only observes.
BROWSER_RECORDER_INSTALL = """(async () => {
  const { setTimeout } = ChromeUtils.importESModule('resource://gre/modules/Timer.sys.mjs');
  const limit = %d;
  const deadline = Date.now() + 10000;
  let first;
  while (!(first = Services.wm.getMostRecentWindow('navigator:browser')) || !first.gBrowser) {
    if (Date.now() > deadline) throw new Error('no browser window to record');
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  if (first.kitsuneHarnessRecorder) return { installed: false };
  const recorder = { events: [], dropped: 0 };
  const F = Ci.nsIWebProgressListener;
  const FLAGS = ['STATE_START', 'STATE_REDIRECTING', 'STATE_TRANSFERRING', 'STATE_STOP', 'STATE_IS_REQUEST',
                 'STATE_IS_DOCUMENT', 'STATE_IS_NETWORK', 'STATE_IS_WINDOW', 'STATE_RESTORING'];
  const push = (event) => {
    event.t = Date.now();
    recorder.events.push(event);
    if (recorder.events.length > limit) { recorder.events.shift(); recorder.dropped += 1; }
  };
  const spec = (uri) => (uri && typeof uri.spec === 'string' ? uri.spec.slice(0, 4096) : null);
  const requestUrl = (request) => {
    try { return spec(request.QueryInterface(Ci.nsIChannel).URI); } catch (e) { return null; }
  };
  const listener = {
    onStateChange(browser, webProgress, request, flags, status) {
      if (!webProgress || !webProgress.isTopLevel) return;
      push({ kind: 'state', browser: browser.browserId, flags: FLAGS.filter(name => flags & F[name]),
             status, url: requestUrl(request) });
    },
    onLocationChange(browser, webProgress, request, location, flags) {
      if (!webProgress || !webProgress.isTopLevel) return;
      push({ kind: 'location', browser: browser.browserId, url: spec(location),
             sameDocument: Boolean(flags & F.LOCATION_CHANGE_SAME_DOCUMENT) });
    },
  };
  const attach = (win) => {
    if (!win.gBrowser || win.kitsuneHarnessRecorder) return;
    win.kitsuneHarnessRecorder = recorder;
    win.gBrowser.addTabsProgressListener(listener);
    for (const [type, kind] of [['TabOpen', 'tab-open'], ['TabClose', 'tab-close']]) {
      win.gBrowser.tabContainer.addEventListener(type, (event) =>
        push({ kind, browser: event.target.linkedBrowser ? event.target.linkedBrowser.browserId : null }));
    }
    push({ kind: 'recorder-attached' });
  };
  for (const win of Services.wm.getEnumerator('navigator:browser')) attach(win);
  Services.obs.addObserver((subject) => attach(subject), 'browser-delayed-startup-finished');
  return { installed: true };
})()""" % MAX_BROWSER_EVENTS

BROWSER_RECORDER_READ = """(() => {
  for (const win of Services.wm.getEnumerator('navigator:browser')) {
    const recorder = win.kitsuneHarnessRecorder;
    if (recorder) return { events: recorder.events.slice(), dropped: recorder.dropped };
  }
  return null;
})()"""


def run_directory(root):
    """This process's run directory under root, named once per process."""
    root = Path(root)
    if root not in _run_directories:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        _run_directories[root] = root / f"kitsune-e2e-{stamp}-{os.getpid()}"
    return _run_directories[root]


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]


def _log_tail(path):
    """(text, truncated): the last MAX_LOG_BYTES of path, or ('', False) if it cannot be read."""
    try:
        with open(path, "rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - MAX_LOG_BYTES))
            return log.read().decode("utf-8", "replace"), size > MAX_LOG_BYTES
    except (OSError, TypeError):
        return "", False


def _alive(process):
    return process is not None and process.poll() is None


class FailureArtifacts:
    """Evidence about one test class's owned browser and bridge, written only on failure.

    watch_bridge/watch_browser name the owned processes before they start;
    install_browser_recorder runs once the browser is up.
    """

    def __init__(self, root, class_name):
        self.root = Path(root)
        self.class_name = class_name
        self.started = now_ms()
        self.bridge = None
        self.browser = None
        self.secrets = []
        self.read_browser_events = None
        self.recorder_error = None
        self.resource_samples = []
        self.setup_snapshot = None
        self.preserved = 0
        self.omitted = 0
        self.dropped_events = 0  # evicted harness/fixture events recorded since self.started
        with _events_lock:
            _watchers.add(self)

    @classmethod
    def from_environment(cls, class_name, environ=os.environ):
        return cls(environ[ARTIFACTS_ENV], class_name) if enabled(environ) else None

    def watch_bridge(self, bridge):
        self.bridge = bridge
        # Line by line, so a failure while the bridge still runs finds its log so far, not an empty buffer.
        bridge.env["PYTHONUNBUFFERED"] = "1"
        self.secrets = [bridge.token] if bridge.token else []

    def watch_browser(self, browser):
        self.browser = browser

    def install_browser_recorder(self, install, read):
        """install() starts the recorder and read() returns its {events, dropped}; failures are kept, not raised."""
        try:
            install()
            self.read_browser_events = read
        except Exception as error:  # diagnostics must not change whether a suite can run
            self.recorder_error = f"{type(error).__name__}: {error}"
            print(f"kitsune e2e artifacts: browser recorder not installed: {self.recorder_error}", file=sys.stderr)
        self.resource_samples.append(self.sample_resources("browser-started"))

    def owned_processes(self):
        """ps rows for the test runner, the bridge process and the browser's process group."""
        roles = {os.getpid(): "test-runner"}
        if self.bridge is not None and _alive(self.bridge.process):
            roles[self.bridge.process.pid] = "bridge"
        browser_process = getattr(self.browser, "process", None)
        if _alive(browser_process):
            # The browser leads its own session, so its group is exactly the processes it started.
            group = subprocess.run(["pgrep", "-g", str(browser_process.pid)], capture_output=True, text=True,
                                   timeout=5, check=False).stdout.split()
            for pid in [browser_process.pid, *map(int, group)]:
                roles.setdefault(pid, "browser-group")
        pids = sorted(roles)[:MAX_OWNED_PROCESSES]
        listing = subprocess.run(
            ["ps", "-o", "pid=,ppid=,pgid=,pcpu=,pmem=,rss=,etime=,state=,comm=", "-p", ",".join(map(str, pids))],
            capture_output=True, text=True, timeout=5, check=False).stdout
        rows = []
        for line in listing.splitlines():
            fields = line.split(None, 8)
            if len(fields) < 9 or int(fields[0]) not in roles:
                continue
            rows.append({"role": roles[int(fields[0])], "pid": int(fields[0]), "ppid": int(fields[1]),
                         "pgid": int(fields[2]), "cpuPercent": float(fields[3]), "memPercent": float(fields[4]),
                         "rssKiB": int(fields[5]), "elapsed": fields[6], "state": fields[7],
                         "executable": os.path.basename(fields[8].strip())})
        return rows

    def sample_resources(self, label):
        sample = {"t": now_ms(), "label": label, "cpuCount": os.cpu_count()}
        try:
            sample["loadAverage"] = list(os.getloadavg())
        except OSError as error:
            sample["loadAverageError"] = str(error)
        try:
            sample["processes"] = self.owned_processes()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            sample["processesError"] = f"{type(error).__name__}: {error}"
        return sample

    def live_snapshot(self):
        """(browser events or None, browser error or None, resource sample) taken now."""
        events, error = None, None
        if self.read_browser_events is None:
            error = self.recorder_error or "browser recorder not installed"
        elif not _alive(getattr(self.browser, "process", None)):
            error = "browser was not running"
        else:
            try:
                events = self.read_browser_events()
                if events is None:
                    error = "browser recorder not found"
            except Exception as read_error:
                error = f"{type(read_error).__name__}: {read_error}"
        return events, error, self.sample_resources("failure")

    def capture_setup_snapshot(self, setup_failed):
        """Class cleanup run before the browser stops: keep live state for a failed setUpClass."""
        if setup_failed:
            self.setup_snapshot = self.live_snapshot()

    def finish_class(self, setup_failed):
        """Class cleanup run after the browser and bridge stop, before the temporary directory goes."""
        if setup_failed:
            self.preserve("setUpClass", self.setup_snapshot)
        with _events_lock:
            _watchers.discard(self)
        if self.omitted:
            print(f"kitsune e2e artifacts: {self.class_name}: {self.omitted} later failure(s) not preserved "
                  f"(limit {MAX_PRESERVED_FAILURES_PER_CLASS} per class)", file=sys.stderr)

    def preserve(self, name, snapshot=None):
        """Write one failure directory; returns it, or None past the per-class limit or if it cannot be written."""
        if self.preserved >= MAX_PRESERVED_FAILURES_PER_CLASS:
            self.omitted += 1
            return None
        self.preserved += 1
        target = run_directory(self.root) / _safe_name(self.class_name) / f"{self.preserved:02d}-{_safe_name(name)}"
        try:
            return self._write(target, name, snapshot or self.live_snapshot())
        except OSError as error:  # a full or unwritable artifact directory must not stop the run
            print(f"kitsune e2e artifacts: {self.class_name} {name}: not written: {error}", file=sys.stderr)
            return None

    def _write(self, target, name, snapshot):
        events, browser_error, resources = snapshot
        target.mkdir(parents=True, exist_ok=False)
        with _events_lock:
            timeline = [event for event in _events if event["t"] >= self.started]
            harness_events = {"limit": _events.maxlen, "droppedSinceClassStart": self.dropped_events,
                              "complete": self.dropped_events == 0,
                              "droppedInProcess": _dropped_in_process}
        browser_events = (events or {}).get("events") or []
        for event in browser_events:
            timeline.append({**event, "source": "browser", "url": sanitize_url(event.get("url"))})
        timeline.sort(key=lambda event: event["t"])
        logs = {}
        for artifact, process in (("browser.log", self.browser), ("bridge.log", self.bridge)):
            text, truncated = _log_tail(getattr(process, "log_path", None))
            (target / artifact).write_text(redact(text, self.secrets), encoding="utf-8")
            logs[artifact] = {"truncated": truncated, "limitBytes": MAX_LOG_BYTES}
        (target / "timeline.jsonl").write_text(
            redact("".join(json.dumps(event, sort_keys=True) + "\n" for event in timeline), self.secrets),
            encoding="utf-8")
        (target / "resources.json").write_text(
            json.dumps(self.resource_samples + [resources], indent=2), encoding="utf-8")
        summary = {
            "class": self.class_name, "failure": name, "classStarted": self.started, "preserved": now_ms(),
            "timelineEvents": len(timeline),
            "browserEvents": {"collected": events is not None, "count": len(browser_events),
                              "dropped": (events or {}).get("dropped", 0), "error": browser_error,
                              "limit": MAX_BROWSER_EVENTS},
            "harnessAndFixtureEvents": harness_events, "logs": logs,
        }
        (target / "summary.json").write_text(redact(json.dumps(summary, indent=2), self.secrets), encoding="utf-8")
        print(f"kitsune e2e artifacts: {self.class_name} {name}: {target}", file=sys.stderr)
        return target
