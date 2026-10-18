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
- [Extension and server compatibility](#extension-and-server-compatibility)
- [Troubleshooting](#troubleshooting)
- [Development setup from a checkout](#development-setup-from-a-checkout)
- [Planned distribution](#planned-distribution)

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

### Claude Desktop, Cursor, and other `mcpServers` clients

Edit the client's JSON config, for example
`~/Library/Application Support/Claude/claude_desktop_config.json` (Claude Desktop
on macOS) or `~/.cursor/mcp.json` (Cursor):

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

### opencode

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

The extension is not part of the Python package, so download it separately.
Until a signed XPI is available (see [Planned distribution](#planned-distribution)),
it is loaded as a **temporary add-on**. Temporary add-ons are removed when the
browser restarts, so you must load the extension again after every restart.

1. Download `kitsune-extension-<version>.zip` for the same version as the
   server from [GitHub Releases](https://github.com/junyeong-nero/kitsune/releases)
   and unzip it. (For an unreleased commit, use the repository ZIP or a clone
   instead; the files are under `extension/`.) The server does not use these
   files. You only need them to load the extension.
2. Open `about:debugging#/runtime/this-firefox` and click
   **Load Temporary Add-on…**.
3. Select `manifest.json` from the unzipped folder (`extension/manifest.json`
   in a repository ZIP or clone). In the macOS file dialog, press Cmd+Shift+G to
   paste a path.
4. Restart or reconnect your MCP client and ask it to call `browser_status`. It
   should report `"extension_connected": true`.

## Pinning a version

Pin a released version with `==`:

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp==0.1.0 kitsune
```

Releases are listed on [PyPI](https://pypi.org/project/kitsune-browser-mcp/#history)
and [GitHub Releases](https://github.com/junyeong-nero/kitsune/releases), which
also carries the matching extension zip.

To run a commit, tag, or branch that is not on PyPI, use the Git source with
`@<ref>` (requires Git):

```bash
claude mcp add --scope user kitsune -- uvx --from git+https://github.com/junyeong-nero/kitsune.git@<ref> kitsune
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

After the server is upgraded, the next MCP connection detects that the running
bridge has a different `build` and replaces it automatically. This briefly
interrupts all clients sharing that bridge; set `KITSUNE_NO_BRIDGE_RESTART=1` in
the MCP server environment to opt out. Then update the extension files to the
same version and click **Reload** on Kitsune in `about:debugging`. A bridge restart
does not reload the extension's code. Reload open tabs if their content scripts
are still outdated. See [Updating Kitsune](guide.md#updating-kitsune) for
bridge lifecycle details.

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
- Browsers: Gecko 142+ (Firefox 142+, Zen 1.23+, LibreWolf).

## Troubleshooting

| Symptom | Check |
|---|---|
| Client reports the server failed to start or `uvx` was not found | Use the full path from `which uvx` (GUI apps). Run the `kitsune-bridge status` command above in a terminal to see uv's error |
| First start times out in the client | Run the `kitsune-bridge status` command once to fill the uv cache, then reconnect |
| uv cannot find `kitsune-browser-mcp` | Check that PyPI is reachable (or your configured index mirrors it) |
| uv cannot fetch a `git+https://` source | Check that `git` is installed and on the client's `PATH`, and that GitHub is reachable |
| An upgrade is not picked up | Restart the MCP client; then refresh with `uvx --refresh …` (see [Upgrading](#upgrading)) |
| The bridge restarts every time a different client starts | Clients use different sources or refs (see [compatibility](#extension-and-server-compatibility)) |
| `extension_connected: false` | Extension loaded? Browser restarted, so the temporary add-on is gone? |
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
extension from the checkout's `extension/manifest.json`.

To test the installed-package path of a branch, run `uvx --from . kitsune` from the
checkout. uv installs a snapshot and does not rebuild it after you edit files;
add `--refresh` to pick up changes.

Run the tests from the repository root:

```bash
uv run python -m unittest discover -s tests -q
for test in tests/*.cjs; do node "$test"; done
```

## Planned distribution

This path is **planned and not available yet**:

- **Signed extension.** A Mozilla-signed XPI or an addons.mozilla.org listing
  would make the extension persist across browser restarts. Installing it would
  still require your approval in the browser. Until then, use the temporary
  add-on steps above. See [TODO](TODO.md).
