# kitsune-browser-mcp

<!-- mcp-name: io.github.junyeong-nero/kitsune -->

The [Kitsune](https://github.com/junyeong-nero/kitsune) MCP server lets AI agents such as Claude
Code, Codex, Pi, and OpenCode use the Firefox, Zen, or LibreWolf browser you already have open,
through the Kitsune browser extension.

This package has no code of its own. It installs
[kitsune-browser](https://pypi.org/project/kitsune-browser/) of the same version, with its MCP
extra, and the commands: `kitsune` starts the server (also named `kitsune-browser-mcp`), and
`kitsune-bridge status` / `stop` inspect or stop the shared bridge.

```bash
claude mcp add --scope user kitsune -- uvx --from kitsune-browser-mcp kitsune
```

[Installation](https://junyeong-nero.github.io/kitsune/installation/) covers the other clients
and the browser extension. To drive the browser from your own Python code, install
kitsune-browser instead ([Python API](https://junyeong-nero.github.io/kitsune/python-api/)).
