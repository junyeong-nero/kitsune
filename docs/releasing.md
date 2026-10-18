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

## Releasing a version

1. Bump `version` in `pyproject.toml`, run `uv lock`, and merge the change to
   `main`.
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
   Node tests, builds the wheel and sdist, publishes them to PyPI, and creates a
   GitHub release with the dist files and `kitsune-extension-<version>.zip`.
5. Verify:
   ```bash
   uvx --refresh --from kitsune-browser-mcp==<version> kitsune-bridge status
   ```

PyPI does not allow re-uploading a version, even after deleting it. If a release
is broken, yank it on PyPI and release a new patch version.

## What is in the package

- **Wheel:** `src/kitsune` only (server, bridge, bundled workflows). The browser
  extension is not part of the Python package; users get it from the GitHub
  release zip or the repository at the same tag.
- **sdist:** `src/kitsune`, `tests`, `README.md`, `LICENSE`, and `pyproject.toml`.

The README is also the PyPI project description, so it uses absolute GitHub URLs
for links and images.
