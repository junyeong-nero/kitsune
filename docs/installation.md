# Installation

Kitsune has two parts that you install separately:

1. **The MCP server.** Your MCP client launches it over stdio. You register it
   once with a command; uv downloads and runs it, so you do not need a checkout.
2. **The browser extension.** You install it in your browser yourself. The
   browser asks for your approval, and nothing is installed silently.

The server starts the shared bridge daemon on demand. The extension connects
to that daemon by itself once both are installed.

- [Prerequisites](#prerequisites)
- [1. Register the MCP server](#1-register-the-mcp-server)
- [2. Install the browser extension](#2-install-the-browser-extension)
- [Pinning a version](#pinning-a-version)
- [Upgrading](#upgrading)
- [Automatic extension updates](#automatic-extension-updates)
- [Extension and server compatibility](#extension-and-server-compatibility)
- [Troubleshooting](#troubleshooting)
- [Development setup from a checkout](#development-setup-from-a-checkout)

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which provides `uvx`.
  uv downloads a supported Python (3.10+) if none is installed.
- Git, only if you install from the Git source instead of PyPI
  (see [Pinning a version](#pinning-a-version)).
- A Firefox-family browser, Gecko 142+: Firefox 142+, Zen 1.23+, or LibreWolf.
- macOS or Linux. Windows is untested.

## 1. Register the MCP server

Every client runs the same stdio command:

```text
uvx --from kitsune-browser-mcp kitsune
```

The first start downloads Kitsune and its dependencies into the uv cache, which
takes a few seconds. Later starts reuse the cached version until you upgrade
(see [Upgrading](#upgrading)). Kitsune is published on PyPI as
`kitsune-browser-mcp`; `kitsune-mcp` is an unrelated project, so do not install
that name.

On macOS, `browser_trusted_click` (needed for example to open "Continue with Google"
popups) needs `uvx --from 'kitsune-browser-mcp[macos]' kitsune` or `brew install cliclick`,
plus Accessibility permission; see [Sign-in popups](guide.md#sign-in-popups-oauthsso).

Optionally, warm the cache and check the install before you register it:

```bash
uvx --from kitsune-browser-mcp kitsune-bridge status
```

This prints `{"status": "stopped"}` (or the running bridge's status) and never
starts a daemon. Running `kitsune` with `--help` does not print help: it is the
MCP server itself, starts the bridge, and waits for an MCP client on stdin.

### Claude Code

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp kitsune
```

`--scope user` makes Kitsune available in every project. Leave it out to register
it only for the current project (`local` scope). Pass environment variables
with `-e`, for example `-e KITSUNE_CAPS=core,network` before the `--`.

### Codex CLI

```bash
codex mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.kitsune]
command = "uvx"
args = ["--from", "kitsune-browser-mcp", "kitsune"]

# Optional
[mcp_servers.kitsune.env]
KITSUNE_CAPS = "core,network"
```

### Pi

With [Pi's built-in MCP support](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md):

```bash
pi mcp add kitsune -- uvx --from kitsune-browser-mcp kitsune
pi mcp list
```

This writes user-level configuration to `~/.pi/agent/mcp.json`. Add `--local`
before `kitsune` to write `.pi/mcp.json` in the current project instead; project
configuration is loaded only after project trust is granted. In a running
session, use `/reload` to pick up the change and `/mcp` to inspect the connection.

You can also edit the JSON config directly using the `mcpServers` example below.
If an MCP adapter extension overrides built-in MCP support, use that extension's
configuration instructions instead.

### Claude Desktop, Cursor, Pi, and other `mcpServers` clients

Edit the client's JSON config, for example
`~/Library/Application Support/Claude/claude_desktop_config.json` (Claude Desktop
on macOS), `~/.cursor/mcp.json` (Cursor), or `~/.pi/agent/mcp.json` (Pi's built-in
MCP support). Merge the entry with any existing servers:

```json
{
  "mcpServers": {
    "kitsune": {
      "command": "uvx",
      "args": ["--from", "kitsune-browser-mcp", "kitsune"]
    }
  }
}
```

Add an `"env": {"KITSUNE_CAPS": "core,network"}` object next to `args` to set
environment variables.

### OpenCode

Edit `~/.config/opencode/opencode.json` (global) or `opencode.json` at a git repo
root (project) by hand.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kitsune": {
      "type": "local",
      "command": ["uvx", "--from", "kitsune-browser-mcp", "kitsune"],
      "enabled": true
    }
  }
}
```

### GUI apps and `PATH`

GUI apps such as Claude Desktop often do not inherit your shell `PATH`. If the
client cannot find `uvx`, use the full path from `which uvx` as the command, for
example `/Users/you/.local/bin/uvx`. Config files do not expand `~`.

Several clients can be configured at once; they share one bridge daemon.
Register them all with the same source and ref (see
[compatibility](#extension-and-server-compatibility)).

## 2. Install the browser extension

The extension is not part of the Python package, so download it separately,
for the **same version** as the server. There are two builds:

| | Signed build (`kitsune-<version>.xpi`) | Developer build (`kitsune-extension-dev-<version>.zip`) |
|---|---|---|
| Install | Once; survives browser restarts | Temporary add-on; load it again after every restart |
| Signed by Mozilla | Yes (self-distributed, not listed on AMO) | No |
| `browser_evaluate`, JS conditions in `browser_sync_barrier` and batch `if`/`while`, HTML mode of editable typing, bundled workflows that use `js` steps | Not available | Available |
| Everything else | Available | Available |

The signed build cannot run JavaScript supplied by the agent, because Mozilla's
[add-on policies](https://extensionworkshop.com/documentation/publish/add-on-policies/)
forbid executing code that is not part of the signed package. Those tools return
an error that names the developer build. `browser_status` (in the `admin` group,
`KITSUNE_CAPS=core,admin`) reports which build is connected (`extension_build`:
`signed` or `dev`).

### Signed build (recommended)

Signed XPIs are attached to releases from 0.2.0 on. For 0.1.0, use the developer
build below.

1. Download `kitsune-<version>.xpi` from
   [GitHub Releases](https://github.com/junyeong-nero/kitsune/releases).
2. Open `about:addons`, click the gear icon, choose **Install Add-on From File…**,
   and select the XPI. Dragging the file onto a browser window also works.
3. Approve the permission prompt. The browser asks every time; nothing is
   installed silently.
4. Restart or reconnect your MCP client and ask it to list your tabs (`browser_tabs`
   with `action: "list"`). With `admin` enabled, `browser_status` should report
   `"extension_connected": true` and `"extension_build": "signed"`.

Signed builds released after 0.2.0 update themselves (see
[Automatic extension updates](#automatic-extension-updates)).

### Developer build

Use this for `browser_evaluate`, or to run an unreleased commit.

1. Download `kitsune-extension-dev-<version>.zip` from
   [GitHub Releases](https://github.com/junyeong-nero/kitsune/releases) and unzip
   it. For 0.1.0 the file is named `kitsune-extension-0.1.0.zip`. For an
   unreleased commit, use the `extension/` folder of a repository ZIP or clone.
2. Open `about:debugging#/runtime/this-firefox` and click
   **Load Temporary Add-on…**.
3. Select `manifest.json` from the unzipped folder (`extension/manifest.json`
   in a repository ZIP or clone). In the macOS file dialog, press Cmd+Shift+G to
   paste a path.
4. With `admin` enabled, call `browser_status`; it should report
   `"extension_build": "dev"`.

Both builds use the same add-on ID. While a developer build is loaded, it
replaces the signed one; after a browser restart the signed build is back.

## Pinning a version

Pin a released version with `==`:

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp==0.7.1 kitsune
```

Releases are listed on [PyPI](https://pypi.org/project/kitsune-browser-mcp/#history)
and [GitHub Releases](https://github.com/junyeong-nero/kitsune/releases), which
also carries the matching signed XPI and developer-build zip.

To run a commit, tag, or branch that is not on PyPI, use the Git source with
`@<ref>` (requires Git). The MCP server's package is in `packages/kitsune-browser-mcp`;
uv builds the code it depends on, kitsune-browser, from the same ref:

```bash
claude mcp add --scope user kitsune -- uvx --from "git+https://github.com/junyeong-nero/kitsune.git@<ref>#subdirectory=packages/kitsune-browser-mcp" kitsune
```

Without `@<ref>`, the Git source follows the latest default-branch commit. Get
the extension from the same ref:
`https://github.com/junyeong-nero/kitsune/archive/<ref>.zip`, or
`git checkout <ref>` in a clone.

## Upgrading

- **Unpinned PyPI** (`--from kitsune-browser-mcp`): uvx keeps using the cached
  version. To move to the newest release, refresh it once, then restart or
  reconnect the MCP client (in Claude Code, `/mcp`):
  ```bash
  uvx --refresh --from kitsune-browser-mcp kitsune-bridge status
  ```
  To check for a new release on every start instead, register
  `--from kitsune-browser-mcp@latest`.
- **Pinned** (`==<version>` or Git `@<ref>`): change the version in the client
  config. For Claude Code, run `claude mcp remove --scope user kitsune`, then run
  the `claude mcp add` command again with the new version.
- **Unpinned Git source**: uv checks for the latest default-branch commit each
  time the MCP server starts (observed with uv 0.7.20).
- **A pip install of 0.6.0 or earlier**, such as a virtual environment for the Python API:
  until 0.6.0 kitsune-browser-mcp held the code that kitsune-browser holds now. pip installs
  kitsune-browser first and then removes the old kitsune-browser-mcp with the files they
  share, so `import kitsune` fails afterwards. Restore them once with
  `pip install --force-reinstall --no-deps kitsune-browser`. The same holds after
  `pip uninstall kitsune-browser-mcp` in an environment that kept 0.6.0. uv and uvx
  are not affected.

After the server is upgraded, the next MCP connection detects that the running
bridge has a different `build` and replaces it automatically. This briefly
interrupts all clients sharing that bridge; set `KITSUNE_NO_BRIDGE_RESTART=1` in
the MCP server environment to opt out. Then update the extension to the same
version: the signed build updates itself (see
[Automatic extension updates](#automatic-extension-updates)); for 0.2.0, or to
update right away, install the new release's XPI the same way as the first time
(it replaces the old version and keeps no separate copy). For the developer
build, load the new release and click **Reload** on Kitsune in `about:debugging`.
A bridge restart does not reload the extension's code. Reload open tabs if their
content scripts are still outdated. See [Updating Kitsune](guide.md#updating-kitsune) for
bridge lifecycle details.

## Automatic extension updates

The signed build checks
`https://github.com/junyeong-nero/kitsune/releases/latest/download/updates.json`
and installs the signed XPI of the latest release. This works from the release
after 0.2.0 on: the 0.2.0 XPI has no update URL, so install the next release's
XPI once by hand (see [Signed build](#signed-build-recommended)); later releases
then arrive automatically. The developer build never updates itself.

- The browser checks about once a day. To check now, open `about:addons`, click
  the gear icon, and choose **Check for Updates**. Updates can be turned off per
  add-on on Kitsune's page in `about:addons` (**Allow automatic updates**).
- A release's `updates.json` is published only after its server is on PyPI, but
  uvx keeps using its cached server (see [Upgrading](#upgrading)). Until you
  refresh it, the extension can be newer than the server; `browser_status` then
  reports an `extension_version` that differs from `bridge_version`.
- If you pin an older server version, turn off automatic updates for Kitsune and
  install the matching XPI from that release.

## Extension and server compatibility

- The server and the extension are developed in the same repository and are
  only tested together at the same commit. Install both from the same release
  (or ref).
  There is no separate compatibility matrix yet.
- `browser_status` reports the bridge's `bridge_version` (package version) and
  `build` (package version plus a hash of the bridge code). An installed
  package does not contain the extension, so its `build` does not change when
  only extension files change. Reload the extension yourself after updating it.
- Use one install source for all clients. A checkout run
  (`uv run --directory …`) also hashes the checkout's `extension/` files, so its
  `build` differs from a `uvx` install of the same commit. If clients with
  different builds share a bridge, each one replaces the bridge when it starts.
  If you need to mix sources, set `KITSUNE_NO_BRIDGE_RESTART=1` in the MCP server
  environments and manage the bridge with `kitsune-bridge stop`.
- `browser_status` also reports the connected extension's `extension_version`
  and `extension_build`. If `extension_version` differs from `bridge_version`,
  upgrade the server (the signed extension updates itself, so it is usually the
  newer one) or install the matching extension.
- Browsers: Gecko 142+ (Firefox 142+, Zen 1.23+, LibreWolf). Firefox release
  and beta builds install only signed extensions, which is why the developer build
  is a temporary add-on.

## Troubleshooting

| Symptom | Check |
|---|---|
| Client reports the server failed to start or `uvx` was not found | Use the full path from `which uvx` (GUI apps). Run the `kitsune-bridge status` command above in a terminal to see uv's error |
| First start times out in the client | Run the `kitsune-bridge status` command once to fill the uv cache, then reconnect |
| uv cannot find `kitsune-browser-mcp` | Check that PyPI is reachable (or your configured index mirrors it) |
| uv cannot fetch a `git+https://` source | Check that `git` is installed and on the client's `PATH`, and that GitHub is reachable |
| An upgrade is not picked up | Restart the MCP client; then refresh with `uvx --refresh …` (see [Upgrading](#upgrading)) |
| The bridge restarts every time a different client starts | Clients use different sources or refs (see [compatibility](#extension-and-server-compatibility)) |
| `extension_connected: false` | Extension installed and enabled in `about:addons`? With the developer build, the browser restarted, so the temporary add-on is gone? |
| A tool says it needs the developer extension build | The signed build cannot run agent-supplied JavaScript; load the [developer build](#developer-build) |
| Stop or inspect the bridge | `uvx --from kitsune-browser-mcp kitsune-bridge status` (or `stop`) with the same source as your clients |

See the guide's [Troubleshooting](guide.md#troubleshooting) table for bridge,
token, and page errors.

## Development setup from a checkout

```bash
git clone https://github.com/junyeong-nero/kitsune ~/workspace/kitsune
cd ~/workspace/kitsune
uv sync
```

Register the checkout instead of the PyPI package so the client runs your
working tree directly:

```bash
claude mcp add --scope user kitsune -- uv run --directory ~/workspace/kitsune kitsune
```

In JSON or TOML configs, use `"command": "uv"` with
`["run", "--directory", "/Users/you/workspace/kitsune", "kitsune"]`. Load the
extension from the checkout's `extension/manifest.json` (that is the developer
build). To try the signed variant locally, build it and load
`build/extension-signed/manifest.json` the same way:

```bash
uv run python scripts/build_extension.py signed
```

To test the installed-package path of a branch, build both packages and run the server
from their wheels; rebuild after you edit files:

```bash
uv build --all-packages
uvx --from dist/kitsune_browser_mcp-<version>-py3-none-any.whl \
    --with dist/kitsune_browser-<version>-py3-none-any.whl kitsune
```

(`uvx --from ./packages/kitsune-browser-mcp kitsune` instead runs the working tree's
code, which uv installs editable.)

Run the tests from the repository root:

```bash
uv run python -m unittest discover -s tests -q
for test in tests/*.cjs; do node "$test"; done
```
