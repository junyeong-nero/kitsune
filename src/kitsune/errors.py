"""Failed bridge results as exceptions, for the Python API (docs/api-reference.md#action-errors)."""

# The connection codes (docs/api-reference.md#connection-diagnosis) and kitsune.bridge_client's
# bridge_disconnected and bridge_version_mismatch: the bridge or the extension was not reached,
# or dropped the request.
CONNECTION_CODES = frozenset({
    "bridge_unavailable", "token_mismatch", "port_conflict", "bridge_disconnected", "bridge_version_mismatch",
    "extension_disconnected", "extension_version_mismatch", "browser_window_unavailable", "host_permission_denied",
})


def failed(result) -> bool:
    """Whether a result reports failure: a non-empty top-level error or ok=false.

    Warnings accompany successes, and batch/parallel/broadcast/workflow results report
    per-command outcomes that may include completed writes, so neither marks the call failed.
    """
    return isinstance(result, dict) and (bool(result.get("error")) or result.get("ok") is False)


class KitsuneError(Exception):
    """A failed bridge result. code, retryable, hint and state are its action error fields, and
    result is the whole reply, such as the outcome of an unmet expect.

    retryable=False means a write may already have taken effect: inspect the page before
    repeating it. Branch on code, not on the message.
    """

    def __init__(self, result: dict):
        self.result = result
        self.code = result.get("code")
        self.retryable = result.get("retryable")
        self.hint = result.get("hint")
        self.state = result.get("state") or {}
        message = result.get("error") or ("Timed out" if result.get("timedOut") else "The bridge answered ok=false")
        if self.code:
            message += f" [{self.code}]"
        if self.hint:
            message += f"\nHint: {self.hint}"
        super().__init__(message)


class ConfirmationRequired(KitsuneError):
    """The action needs the user's approval and nothing ran: ask-before-act held it
    (confirmation_required), it closes or, with protect_user_tabs, navigates a tab no client
    opened (user_tab), or it acts on the tab the user is viewing (user_viewing). Once the user
    approves, repeat it with confirm=token within expires_in seconds; a token works once."""

    @property
    def token(self) -> str | None:
        return self.state.get("confirmToken")

    @property
    def expires_in(self) -> int | None:
        return self.state.get("expiresInSeconds")


class ConnectionFailed(KitsuneError, ConnectionError):
    """The bridge or the extension was not reached, or dropped the request; retryable says
    whether the request may have run."""


class TimedOut(KitsuneError, TimeoutError):
    """A command, page load or wait did not finish in time; a write may have run (retryable)."""


def raise_for(result):
    """Return result, or raise the KitsuneError subclass of its failure."""
    if not failed(result):
        return result
    code = result.get("code")
    # read and draft refuse user tabs without a token: those cannot be approved.
    if code in ("confirmation_required", "user_tab", "user_viewing") and (result.get("state") or {}).get("confirmToken"):
        raise ConfirmationRequired(result)
    if code in CONNECTION_CODES:
        raise ConnectionFailed(result)
    if code == "timeout" or result.get("timedOut") is True:
        raise TimedOut(result)
    raise KitsuneError(result)
