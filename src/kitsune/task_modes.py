"""Per-request bridge headers and the user-tab permission (#141). Task modes were removed in #525."""


# Authoritative per-request permission; absent means read-only. Old wire headers grant nothing.
USER_TABS_HEADER = "X-Kitsune-User-Tabs"
# The tool a browser_tools_call runs (#227): the audit log records the call came through it.
GATEWAY_TOOL_HEADER = "X-Kitsune-Gateway-Tool"
# The id of the client that sent a request (BridgeClient.client_id, #303): audit entries record it.
CLIENT_HEADER = "X-Kitsune-Client"
# The id of the MCP client a request comes from (BridgeClient.owner_id, #325): unlike CLIENT_HEADER it
# survives an MCP server restart, so the bridge records the tabs it opens as that client's.
OWNER_HEADER = "X-Kitsune-Owner"
SESSION_HEADER = "X-Kitsune-Session"
CLIENT_NAME_HEADER = "X-Kitsune-Client-Name"
# A random secret per MCP server process, on each of its bridge requests (#376). The bridge keeps only its
# hash, with each confirm token issued to one of those requests.
APPROVER_HEADER = "X-Kitsune-Approver"
# "elicitation" on the repeat of a held call the user approved in the MCP client's prompt (#376). The
# bridge records it only when the request's APPROVER_HEADER matches the one the token was issued to.
APPROVAL_HEADER = "X-Kitsune-Approval"
# The MCP tool a request belongs to and that call's random id (#525): the bridge checks the user's
# per-tool approvals against them (kitsune.tool_approvals).
TOOL_HEADER = "X-Kitsune-Tool"
CALL_HEADER = "X-Kitsune-Call"
# The characters and length the bridge accepts in CLIENT_HEADER, OWNER_HEADER and CALL_HEADER.
CLIENT_ID_PATTERN = r"[A-Za-z0-9._:-]{1,64}"


def user_tabs(value: str | None) -> str:
    """Canonical permission shared by config, SDK and HTTP; invalid explicit values never opt out."""
    if value is None:
        return "read-only"
    normalized = value.strip().lower() if isinstance(value, str) else None
    if normalized not in {"read-only", "writable"}:
        raise ValueError(f"Invalid user-tab permission {value!r}: KITSUNE_USER_TABS / "
                         f"{USER_TABS_HEADER} must be read-only or writable")
    return normalized
