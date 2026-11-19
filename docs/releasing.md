# Releasing

Two PyPI distributions share one version:

- **`kitsune-browser`** (repository root): the `kitsune` package and the `kitsune-bridge` command.
- **`kitsune-browser-mcp`** ([`packages/kitsune-browser-mcp`](../packages/kitsune-browser-mcp)):
  no code; pins `kitsune-browser[mcp]` and provides the `kitsune` commands. MCP clients install this.

Releases run from [`.github/workflows/release.yml`](../.github/workflows/release.yml) with
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/); no API token is stored.

## One-time setup

1. **PyPI pending publisher.** Sign in to <https://pypi.org>, open
   *Account settings → Publishing → Add a new pending publisher*, and enter:

   | Field | Value |
   |---|---|
   | PyPI project name | `kitsune-browser` |
   | Owner | `junyeong-nero` |
   | Repository name | `kitsune` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   Both projects need this publisher; `kitsune-browser-mcp` already has it.
2. **TestPyPI (optional, recommended for the first release).** Repeat on
   <https://test.pypi.org> with environment name `testpypi`.
3. **GitHub environments.** In the repository, open *Settings → Environments*
   and create `pypi` (and `testpypi`). Adding yourself as a required reviewer on
   `pypi` makes every publish wait for your approval.
4. **AMO signing credentials.** Sign in to <https://addons.mozilla.org>, open
   [API credentials](https://addons.mozilla.org/developers/addon/api/key/), and
   generate a JWT issuer and secret. In the repository, create a GitHub
   environment named `amo` and add them as environment secrets
   `AMO_JWT_ISSUER` and `AMO_JWT_SECRET`. Never commit them, post them in issues,
   or paste them into chats; regenerate them on AMO if they leak. The first
   signed upload creates the add-on (ID `kitsune@junyeong-nero.github.io`) as
   self-distributed (unlisted) on your AMO account.

## Releasing a version

1. Bump `version` in `pyproject.toml`, in `packages/kitsune-browser-mcp/pyproject.toml`
   (its `version` and both `kitsune-browser[...]==` pins), `extension/manifest.json`, and both
   `version` fields of `server.json` (tests check they match), run `uv lock`, and
   merge the change to `main`. AMO accepts
   each extension version only once, so a failed signing also needs a new version.
2. Optional dry run: build both distributions and check them locally.
   ```bash
   rm -rf dist && uv build --all-packages && uvx twine check dist/*
   uvx --find-links ./dist --from kitsune-browser-mcp==<version> kitsune-bridge status
   ```
3. Optional TestPyPI run: *Actions → Release → Run workflow* on `main`, then
   ```bash
   uvx --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ \
     --from kitsune-browser-mcp kitsune-bridge status
   ```
   A version can be uploaded to each index only once; bump the version (or use a
   pre-release such as `0.2.0rc1`) to upload again.
4. Tag and push:
   ```bash
   git switch main && git pull
   git tag v<version>
   git push origin v<version>
   ```
   The workflow checks that the tag matches `pyproject.toml`, runs the Python and
   Node tests, builds the wheel and sdist, and builds both extension variants
   (`scripts/build_extension.py`). The signed variant must pass
   `web-ext lint --self-hosted` with no warnings. It is then signed on AMO
   (unlisted, `amo` environment). Only after signing succeeds does the workflow
   publish to PyPI and create a GitHub release, marked as latest, with the dist
   files, `kitsune-<version>.xpi` (signed), `updates.json` (see
   [Extension updates](#extension-updates)), and
   `kitsune-extension-dev-<version>.zip` (developer build). After the PyPI
   publish, it also publishes `server.json` to the
   [MCP Registry](https://registry.modelcontextprotocol.io) as
   `io.github.junyeong-nero/kitsune`, authenticated with GitHub OIDC. The registry
   checks for the `mcp-name:` comment in the README kitsune-browser-mcp publishes to PyPI
   (`packages/kitsune-browser-mcp/README.md`), so keep it.

   Automated signing usually takes minutes; the job waits up to 30 minutes. If
   AMO selects the version for manual review and the job times out, nothing is
   published. Once AMO approves it, download the signed XPI from the
   [Developer Hub](https://addons.mozilla.org/developers/addons), then run the
   remaining jobs manually or attach the XPI to the release yourself. In that
   case, rename it to `kitsune-<version>.xpi`, run
   `python3 scripts/update_manifest.py kitsune-<version>.xpi`, and attach the
   resulting `updates.json` too.
5. Verify:
   ```bash
   uvx --refresh --from kitsune-browser-mcp==<version> kitsune-bridge status
   ```

PyPI does not allow re-uploading a version, even after deleting it. If a release
is broken, yank it on PyPI and release a new patch version.

## Extension updates

The signed build's `update_url` is
`https://github.com/junyeong-nero/kitsune/releases/latest/download/updates.json`, written by
`scripts/update_manifest.py` after the PyPI publish succeeds
([format](https://extensionworkshop.com/documentation/manage/updating-your-extension/)).

- It follows GitHub's **latest** release; a release marked latest by hand must carry an
  `updates.json` too.
- To withdraw an update, delete that release's `updates.json`. Firefox never downgrades, so fix
  already-updated copies with a new patch release.

## What is in the packages

- **kitsune-browser wheel:** `src/kitsune` only (Python API, tools, server, bridge, bundled
  workflows). The browser extension is not part of the Python packages; users get it from
  the GitHub release (signed XPI or developer zip) or the repository at the same tag.
- **kitsune-browser sdist:** `src/kitsune`, `tests`, `README.md`, `LICENSE`, and `pyproject.toml`.
- **kitsune-browser-mcp wheel and sdist:** metadata only: the pinned dependency, the
  commands, and `packages/kitsune-browser-mcp/README.md`.

Each README is its PyPI project description, so it uses absolute GitHub URLs
for links and images.

## Extension builds

`extension/` is the developer build as-is. `scripts/build_extension.py signed`
removes every `// @kitsune-dev-only:start` … `// @kitsune-dev-only:end` region,
sets `KITSUNE_BUILD` to `'signed'`, stamps the manifest version, adds the
`update_url` (lint it with `--self-hosted`, as AMO does for unlisted uploads,
because `update_url` is otherwise an error), and fails if any
`eval`, `new Function`, `executeScript({ code })`, `innerHTML`/`outerHTML`
assignment, `insertAdjacentHTML`, `document.write`, or string timer remains.
Mozilla's add-on policy forbids executing code that is not in the package, and
self-distributed add-ons can be reviewed and blocked at any time. Keep new
features that run agent-supplied code inside dev-only regions, and implement
fixed page operations as named `content.js` handlers instead of code strings.
