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


def command_wait_seconds(cmd):
    """Longest time a batch command may spend waiting by design, in seconds."""
    if not isinstance(cmd, dict):
        return 0
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
    return actionability_wait_seconds(action, cmd)


def batch_wait_seconds(commands):
    """Batch commands run one after another, so their waits add up."""
    return sum(command_wait_seconds(cmd) for cmd in commands)
