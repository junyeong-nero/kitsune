# Development

## Setup from a checkout

```bash
git clone https://github.com/junyeong-nero/kitsune ~/workspace/kitsune
cd ~/workspace/kitsune
uv sync
```

Register the checkout so the client runs your working tree:

```bash
claude mcp add --scope user kitsune -- uv run --directory ~/workspace/kitsune kitsune
```

In JSON or TOML configs, use `"command": "uv"` with
`["run", "--directory", "/Users/you/workspace/kitsune", "kitsune"]`. Load the extension from
`extension/manifest.json` (the developer build). For the signed variant:

```bash
uv run python scripts/build_extension.py signed   # then load build/extension-signed/manifest.json
```

To test the installed-package path, build both wheels and run from them:

```bash
uv build --all-packages
uvx --from dist/kitsune_browser_mcp-<version>-py3-none-any.whl \
    --with dist/kitsune_browser-<version>-py3-none-any.whl kitsune
```

## Tests

```bash
uv run python -m unittest discover -s tests -q
for test in tests/*.cjs; do node "$test"; done
```

Python tests cover the MCP tool surface, snapshots, waits and the bridge; the Node harnesses
cover content-script behavior. For an end-to-end check, follow [Try it](try-it.md) with the
extension loaded. Bug reports should include the browser version, `browser_status`'s `build`,
the tool calls and expected versus actual behavior, on a minimal page with secrets redacted.

### Tool definition budgets

Tool definitions are resent with every model request, so their size is tracked.
`uv run python scripts/tool_schema_size.py` reports each tool's compact JSON size; a test fails
until `--update` records changes in `tests/tool_schema_sizes.json`, and when totals exceed the
budgets in that script. Raise a budget only deliberately.

Conventions the tests enforce:

- The `core` tools are Playwright MCP's, published verbatim from
  `src/kitsune/playwright_tools.json` (`tests/test_playwright_tools.py`).
- Kitsune-owned schemas omit pydantic `title`s and publish optional parameters as plain types
  without `null`/`""` defaults; calls are still validated by pydantic
  (`tests/test_tool_schema_titles.py`, `tests/test_tool_schema_optionals.py`).
- Parameters are documented in the schema (`Annotated[..., Field(description=...)]`), not in
  `Args:` blocks. A tool's first paragraph says what it does; the shared action contract (`ref`,
  `element`, `wait`, `expect`, `confirm_token`, `first`) is documented once on `browser_check`
  (`tests/test_tool_first_paragraph.py`).

### Real-browser suites

`tests/test_real_browser*.py` run against a real Firefox, Zen or LibreWolf and are skipped unless
`KITSUNE_E2E_BROWSER` names the binary:

```bash
KITSUNE_E2E_BROWSER=/Applications/Zen.app/Contents/MacOS/zen \
  uv run python -m unittest discover -s tests -p 'test_real_browser*.py' -v
```

Use `-p <file>` to run one suite. Each class starts a headless browser on a temporary profile with
its own bridge and fixture site on spare ports; your profiles and bridge are untouched. See
`tests/real_browser_harness.py` for adding a suite.

Set `KITSUNE_E2E_ARTIFACTS=/some/dir` to keep evidence from failures: each failing test writes a
directory with a `timeline.jsonl` (bridge requests, fixture requests and navigation events on one
clock), tails of `browser.log` and `bridge.log` (tokens redacted), `resources.json` and
`summary.json`.

## Releasing

See [Releasing](https://github.com/junyeong-nero/kitsune/blob/main/docs/releasing.md).

## Layout

```
extension/          WebExtension (manifest v2, Gecko)
packages/kitsune-browser-mcp/  The MCP server's PyPI package: pins kitsune-browser[mcp], adds the commands
src/kitsune/        The kitsune-browser package
  server.py         MCP server: registers the tools and publishes the default or legacy list
  tools.py          Kitsune's own agent tools; playwright_tools.py has the core group
  browser.py        Python API (kitsune.connect, Browser, Tab); errors.py has its exceptions
  bridge_client.py  Bridge requests and connection errors; starts the bridge on demand
  bridge.py         Bridge daemon (HTTP API + WebSocket)
  workflows/        Named workflows for browser_workflow
docs/               Documentation site (mkdocs.yml), served under /docs/
landing/            Landing page at the site root; scripts/build_site.py builds both
```

## Credits

Kitsune started as a fork of [ZenLink](https://github.com/JayQuan-McCleary/ZenLink) and
[ZenLink-MCP](https://github.com/JayQuan-McCleary/ZenLink-MCP) by Jay-Quan (MIT).

## License

MIT. See [LICENSE](https://github.com/junyeong-nero/kitsune/blob/main/LICENSE).
