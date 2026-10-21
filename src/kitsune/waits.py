"""Wait bounds shared by the MCP server and the bridge, so transport timeouts outlast the waits."""

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
# CONTAINER_MIN_DEADLINE_SECONDS; a workflow at WORKFLOW_DEADLINE_SECONDS (the MCP server
# cannot see a workflow's commands).
CONTAINER_MIN_DEADLINE_SECONDS = 300
WORKFLOW_DEADLINE_SECONDS = 300
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
