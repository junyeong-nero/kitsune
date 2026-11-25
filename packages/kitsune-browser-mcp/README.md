# kitsune-browser-mcp

<!-- mcp-name: io.github.junyeong-nero/kitsune -->

[Kitsune](https://github.com/junyeong-nero/kitsune) connects your MCP client to the Firefox,
Zen or LibreWolf browser you already have open.

1. [Install the browser extension](https://junyeong-nero.github.io/kitsune/docs/installation/#1-install-the-browser-extension).
2. Register the MCP server with your agents:

    <pre><code><a href="https://docs.astral.sh/uv/getting-started/installation/" title="Install uv (includes uvx)">uvx</a> kitsune-browser-mcp@latest setup</code></pre>

    Select your clients with **Space**, then press **Enter**. Restart them, or run `/reload` in Pi.

See [Installation](https://junyeong-nero.github.io/kitsune/docs/installation/) for manual setup and
`kitsune-bridge` commands. For Python scripts, use [kitsune-browser](https://junyeong-nero.github.io/kitsune/docs/python-api/) instead.
