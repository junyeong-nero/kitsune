# Windows

Kitsune runs natively on Windows with the same extension, MCP server and tab
handoff as on macOS and Linux. Install it as in [Installation](installation.md), with the
differences below.

## Turn off Python scripting

`browser_run_python` does not run on Windows yet. Start the MCP server with
`KITSUNE_SCRIPTING=0`, which gives the agent the [direct browser tools](tools.md#core-tools)
instead. For Claude Code:

```powershell
claude mcp add --scope user -e KITSUNE_SCRIPTING=0 kitsune -- uvx --from kitsune-browser-mcp kitsune
```

Other clients set the variable in their MCP server entry, as shown for each client in
[Add Kitsune to your agent](installation.md#3-add-kitsune-to-your-agent).

## The data directory is private

The bridge keeps its token, log and lock files in `KITSUNE_HOME` (default
`%USERPROFILE%\.kitsune`). Windows does not honor the POSIX `0700` mode, so the bridge sets
an access list instead: only your account, SYSTEM and Administrators can open the directory
and the token file.

It replaces the directory's access list only when the bridge creates the directory or finds it
empty. If `KITSUNE_HOME` points at a folder that already holds other files, that folder keeps its
permissions, the bridge prints a warning, and only the token file is made private. Point
`KITSUNE_HOME` at a new or empty directory to keep the log and state private too.

The bridge's listeners bind only to `127.0.0.1` and refuse to share their ports with another
program.

## Limitations

- **No `browser_run_python`**: see [above](#turn-off-python-scripting).
- **OS-level input**: native keyboard and drag input (`browser_trusted_type`,
  `browser_trusted_press_key`, `browser_trusted_drag`) are macOS only and return
  `unsupported_platform` on Windows.
- **Building the signed extension**: `scripts/build_extension.py signed` does not yet produce a
  correct signed build on Windows; `tests/test_build_extension.py` fails there. The released
  `.xpi` is built on Linux and is not affected.

## Verifying a checkout

Contributors can run the Windows bridge checks from a checkout. Importing the bridge creates its
data directory, so point `KITSUNE_HOME` at a disposable one:

```powershell
$env:KITSUNE_HOME = Join-Path $env:TEMP ('kitsune-tests-' + [guid]::NewGuid())
$env:KITSUNE_TOKEN = 'synthetic-test-token-not-a-real-secret'
uv run --frozen python -m unittest discover -s tests -p test_windows_lifecycle.py -v
uv run --frozen python -m unittest discover -s tests -p test_bridge_lifecycle.py -q
uv run --frozen python -m unittest discover -s tests -p test_bridge_security.py -q
uv run --frozen python -m unittest discover -s tests -p test_bridge_websocket.py -q
node tests/tab_handoff_races.cjs
```

They cover lock contention, PIDs that stay readable while locked, restart after a crash,
refusing to stop an unrelated or recycled PID, the private access lists, signal cleanup,
non-Unicode parent output, independent homes and port takeover. The `Bridge platforms` workflow
runs them on Windows and Linux with Python 3.12 and 3.14.

The real extension's approve, decline, take-back and reconnect journeys run against a disposable
browser profile:

```powershell
$env:KITSUNE_E2E_BROWSER = 'C:\Program Files\Mozilla Firefox\firefox.exe'
uv run --frozen python -m unittest discover -s tests -p test_real_browser_handoff.py -k test_popup_approval -k test_popup_decline -k test_take_back -k test_session_end -k test_client_disconnect -k test_a_bridge_reconnect -k test_an_extension_reload -k test_a_grant_to_one_client -v
```
