"""Wait bounds shared by the bridge and its clients (the MCP server and the Python API), so
transport timeouts outlast the waits."""

import math

# The default expect.timeout; matches bridge.OUTCOME_TIMEOUT_MS.
OUTCOME_TIMEOUT_MS = 5000

# With an opt-in `wait`, the content script waits up to `timeout` ms for these actions' targets.
ACTIONABILITY_WAIT_ACTIONS = frozenset({
    "click", "doubleClick", "hover", "drag",
    "type", "fill", "setEditableContent", "selectOption", "checkBox", "keypress",
})


def actionability_wait_seconds(action, params):
    """Time an action may spend waiting for its target before dispatch; 0 in immediate mode."""
    if action not in ACTIONABILITY_WAIT_ACTIONS or not params.get("wait"):
        return 0
    return params.get("timeout", 10000) / 1000


def expect_wait_seconds(cmd):
    """Bounded post-dispatch outcome check of a command with `expect` (default 5000 ms)."""
    expect = cmd.get("expect")
    if not isinstance(expect, dict):
        return 0
    timeout = expect.get("timeout", 5000)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
        return 0  # the bridge rejects it before dispatch
    return timeout / 1000 + 5


def command_wait_seconds(cmd):
    """Longest time a batch command may spend waiting by design, in seconds."""
    if not isinstance(cmd, dict):
        return 0
    return _command_wait_seconds(cmd) + expect_wait_seconds(cmd)


def _command_wait_seconds(cmd):
    action = cmd.get("action")
    if action in ("waitForElement", "waitForUrl", "waitForTitle"):
        return cmd.get("timeout", 10000) / 1000
    if action == "waitForResult":
        return cmd.get("timeout", 15000) / 1000
    if action == "newTab" and cmd.get("wait"):
        return cmd.get("timeout", 15000) / 1000
    if action == "sleep":
        return cmd.get("ms", 1000) / 1000
    if action == "parallel":
        # Sequences run concurrently; the slowest one bounds the command.
        return max((batch_wait_seconds(seq) for seq in cmd.get("sequences", [])), default=0)
    if action == "sequence":
        return batch_wait_seconds(cmd.get("do", []))
    if action == "retry":
        return _retry_wait_seconds(cmd)
    if action == "if":
        # Only one branch runs.
        return max(batch_wait_seconds(cmd.get("then", [])), batch_wait_seconds(cmd.get("else", [])))
    if action == "while":
        return max(0, _int(cmd.get("maxIterations"), 20)) * batch_wait_seconds(cmd.get("do", []))
    if action == "try":
        return batch_wait_seconds(cmd.get("do", [])) + batch_wait_seconds(cmd.get("catch", []))
    return actionability_wait_seconds(action, cmd)


def _retry_wait_seconds(cmd):
    # Every attempt may wait in full, and a failed attempt sleeps backoffMs * 2**i after it.
    attempts = max(1, _int(cmd.get("maxAttempts"), 3))
    backoff_ms = max(0, _int(cmd.get("backoffMs"), 500))
    return attempts * command_wait_seconds(cmd.get("command") or {}) + backoff_ms * (2 ** attempts - 1) / 1000


def _int(value, default):
    """A count as the bridge parses it; the default when missing or unparseable."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def batch_wait_seconds(commands):
    """Batch commands run one after another, so their waits add up."""
    return sum(command_wait_seconds(cmd) for cmd in commands)


# A batch or parallel stops at its waits plus CONTAINER_MARGIN_SECONDS, never before
# CONTAINER_MIN_DEADLINE_SECONDS; a workflow at WORKFLOW_DEADLINE_SECONDS and a resumed call at
# RESUME_DEADLINE_SECONDS (the MCP server cannot see a workflow's or a checkpoint's commands).
CONTAINER_MIN_DEADLINE_SECONDS = 300
WORKFLOW_DEADLINE_SECONDS = 300
RESUME_DEADLINE_SECONDS = 300
CONTAINER_MARGIN_SECONDS = 15


def batch_deadline_seconds(commands):
    """When the bridge cancels a batch and reports its partial progress."""
    return max(CONTAINER_MIN_DEADLINE_SECONDS, batch_wait_seconds(commands) + CONTAINER_MARGIN_SECONDS)


def container_settle_seconds(deadline):
    """How long the bridge waits for a container: its deadline, then the cancelled step settling."""
    return deadline + CONTAINER_MARGIN_SECONDS


def container_request_seconds(deadline):
    """MCP request timeout of a container call: past the bridge's settle time, so the partial report arrives."""
    return container_settle_seconds(deadline) + CONTAINER_MARGIN_SECONDS


# browser_resume of a direct call waits up to `wait` seconds for the user (#376), capped. The MCP
# server does that wait itself, between bridge requests, so no bridge request outlasts its own timeout.
RESUME_WAIT_SECONDS = 120
RESUME_MAX_WAIT_SECONDS = 300


def resume_wait_seconds(wait):
    """A browser_resume wait as the MCP server applies it: 0 to RESUME_MAX_WAIT_SECONDS."""
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait):
        return RESUME_WAIT_SECONDS
    return min(max(0, wait), RESUME_MAX_WAIT_SECONDS)


# A direct action's request timeout before its waits.
ACTION_REQUEST_SECONDS = 15

# The extension's navigateTab waits up to NAVIGATION_LOAD_SECONDS (waitForTabLoad's default) for
# the load, then asks the page for its URL for up to PAGE_URL_SECONDS (PAGE_URL_TIMEOUT_MS) before
# it reports navigation_stalled (#366). A navigate request outlasts both by NAVIGATION_MARGIN_SECONDS,
# so the caller gets that code and its hint, not a client-side timeout.
NAVIGATION_LOAD_SECONDS = 15
PAGE_URL_SECONDS = 0.5
NAVIGATION_MARGIN_SECONDS = 10
NAVIGATE_REQUEST_SECONDS = NAVIGATION_LOAD_SECONDS + PAGE_URL_SECONDS + NAVIGATION_MARGIN_SECONDS


def action_request_seconds(wait, timeout, expect, base_seconds=ACTION_REQUEST_SECONDS):
    """Request timeout of a direct action: base_seconds (NAVIGATE_REQUEST_SECONDS for a navigate),
    plus its actionability wait (wait, timeout ms) and, with expect, its outcome check
    (expect.timeout ms) and 45 s for the reply."""
    seconds = base_seconds
    if wait:
        seconds += timeout / 1000
    if expect is not None:
        wait_ms = expect.get("timeout", OUTCOME_TIMEOUT_MS) if isinstance(expect, dict) else 0
        if isinstance(wait_ms, bool) or not isinstance(wait_ms, (int, float)) or not math.isfinite(wait_ms):
            wait_ms = 0  # the bridge rejects it before dispatch
        seconds += max(0, wait_ms) / 1000 + 45
    return seconds
