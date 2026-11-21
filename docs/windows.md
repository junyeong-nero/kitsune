# Native Windows bridge support

This checkout adds native Windows support to Kitsune's Python bridge. It reuses
the existing Firefox extension, MCP protocol, tab ownership, and handoff UI.
OS-level trusted keyboard/mouse features remain platform-specific; this change
does not implement the macOS native-input features on Windows.

## Install this development checkout

Use Windows 11, Python 3.12 or newer, uv, and an existing supported Firefox.
From the checkout:

```powershell
uv sync --frozen
uv run kitsune-bridge status
```

For an MCP client, run this checkout with `KITSUNE_SCRIPTING=0` to use direct
browser tools. The optional `browser_run_python` worker still uses POSIX
process-group cleanup and is outside this bridge port. The upstream default
scripting surface is therefore not supported by this Windows preview.
Use `uv run --directory C:\path\to\kitsune --frozen kitsune` as the client
command; `uvx --from kitsune-browser-mcp kitsune` would fetch the unpatched release.

The Windows-only dependencies provide process-identity checks and Windows ACLs.
The configured `KITSUNE_HOME` is private application storage: the bridge secures
that directory and its credential file for the current user, Administrators,
and SYSTEM. Choose a dedicated directory, not a general documents/profile root.
Raw tokens stay local. Bridge listeners bind only to loopback and use exclusive
Windows socket ownership.

Follow [installation](installation.md) for the signed extension matching this
checkout's package version. Installing the signed extension into an already
open Firefox profile does not require copying that profile or its cookies.
Existing-tab writes still require the extension's handoff approval and can be
taken back from its popup. This checkout does not install an extension into a
personal profile or register an MCP server globally for you.

An agent host's native Computer Use browser-policy support is separate from
Kitsune. This port does not modify a host's URL permissions or make an explicitly
refused native action available. Use an approved MCP integration and the host's
site-permission controls before connecting a real account. The tests below use
only disposable profiles, synthetic content, and isolated bridge homes.

## Verification

For the Python tests, set a disposable parent home because importing the bridge
initializes its data directory:

```powershell
$env:KITSUNE_HOME = Join-Path $env:TEMP ('kitsune-tests-' + [guid]::NewGuid())
$env:KITSUNE_TOKEN = 'synthetic-test-token-not-a-real-secret'
uv run --frozen python -m unittest discover -s tests -p test_windows_lifecycle.py -v
uv run --frozen python -m unittest discover -s tests -p test_bridge_lifecycle.py -q
uv run --frozen python -m unittest discover -s tests -p test_bridge_security.py -q
uv run --frozen python -m unittest discover -s tests -p test_bridge_websocket.py -q
node tests/tab_handoff_races.cjs
```

Native subprocess checks cover lock contention, readable locked PIDs, restart
after a crash, unrelated/recycled PID refusal, private token/data ACLs, signal
cleanup, non-Unicode parent output, independent homes, and port takeover refusal.
The existing security suite checks bearer authentication, Host validation,
WebSocket origin rejection, single-daemon races, and authenticated shutdown.

For the real extension's approve/decline/revoke/reconnect journeys:

```powershell
$env:KITSUNE_E2E_BROWSER = 'C:\Program Files\Mozilla Firefox\firefox.exe'
uv run --frozen python -m unittest discover -s tests -p test_real_browser_handoff.py -k test_popup_approval -k test_popup_decline -k test_take_back -k test_session_end -k test_client_disconnect -k test_a_bridge_reconnect -k test_an_extension_reload -k test_a_grant_to_one_client -v
```

These tests create their own browser instance/profile and temporary extension,
use nondefault bridge ports, and exercise the real extension popup. Test cleanup
targets only the owned process identities and their descendants. No private
logged-in page is part of the acceptance test, and the tests do not establish
that Claude's export/email retrieval workflow has completed.

The platform workflow runs the bridge checks on Windows and Linux, Python 3.12
and 3.14. A configured CI matrix is not evidence of a completed CI run.

The full upstream suite is not green on this Windows checkout. The broad
fail-fast run stopped at `test_signed_build_has_no_code_execution` after 306
tests; the signed-build marker was missing from the generated content script.
That packaging path and the POSIX scripting worker require follow-up. The
downloaded upstream signed extension and the isolated development-extension
handoff tests are separate artifacts.
