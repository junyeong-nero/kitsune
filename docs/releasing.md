# Releasing

Kitsune is published to PyPI as **`kitsune-browser-mcp`** (the import package
and commands stay `kitsune` and `kitsune-bridge`). `kitsune-mcp` on PyPI is an
unrelated project.

Releases run from [`.github/workflows/release.yml`](../.github/workflows/release.yml)
with [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/), so no
API token is stored in the repository or in GitHub secrets.

## One-time setup

1. **PyPI pending publisher.** Sign in to <https://pypi.org>, open
   *Account settings → Publishing → Add a new pending publisher*, and enter:

   | Field | Value |
   |---|---|
   | PyPI project name | `kitsune-browser-mcp` |
   | Owner | `junyeong-nero` |
   | Repository name | `kitsune` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   The first successful publish creates the project and turns the pending
   publisher into a normal one.
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

1. Bump `version` in `pyproject.toml`, `extension/manifest.json`, and both
   `version` fields of `server.json` (tests check they match), run `uv lock`, and
   merge the change to `main`. AMO accepts
   each extension version only once, so a failed signing also needs a new version.
2. Optional dry run: build and check locally.
   ```bash
   rm -rf dist && uv build && uvx twine check dist/*
   uvx --from ./dist/kitsune_browser_mcp-<version>-py3-none-any.whl kitsune-bridge status
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
   checks for the `mcp-name:` comment in the README published to PyPI, so keep it.

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

The signed build's manifest sets `browser_specific_settings.gecko.update_url` to
`https://github.com/junyeong-nero/kitsune/releases/latest/download/updates.json`.
The `github-release` job writes that file with `scripts/update_manifest.py`: one
entry for the release's version, the download link of its
`kitsune-<version>.xpi`, the XPI's SHA-256 hash, and the manifest's
`strict_min_version`
([update manifest format](https://extensionworkshop.com/documentation/manage/updating-your-extension/)).
Installed copies check about once a day and update when the listed version is
newer than theirs.

- The URL follows GitHub's **latest** release. The workflow marks each new
  release as latest; any release marked latest by hand must also carry an
  `updates.json`, or update checks fail until the next release.
- The job runs only after the PyPI publish succeeds, so the extension update is
  never offered before the matching server can be installed.
- The update URL is read from the installed version. Moving `updates.json`
  elsewhere would require one release that points at the new URL first.

To withdraw a release's extension update, delete `updates.json` from that release
on GitHub (or replace it with the previous release's file). This stops further
updates; copies that already updated keep that version, because Firefox never
downgrades through an update manifest. Keep the XPI asset while `updates.json`
links to it. To fix the copies that already updated, release a new patch
version.

## What is in the package

- **Wheel:** `src/kitsune` only (server, bridge, bundled workflows). The browser
  extension is not part of the Python package; users get it from the GitHub
  release (signed XPI or developer zip) or the repository at the same tag.
- **sdist:** `src/kitsune`, `tests`, `README.md`, `LICENSE`, and `pyproject.toml`.

The README is also the PyPI project description, so it uses absolute GitHub URLs
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
