"""Core tool file names resolve against the client's workspace root (#225).

KITSUNE_OUTPUT_DIR wins, then the MCP client's first file root, then the MCP server's
working directory; absolute names are kept.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import anyio
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ErrorData, ListRootsResult, Root

from kitsune import playwright_tools, server


def _environment(**settings):
    """os.environ without KITSUNE_OUTPUT_DIR, plus settings."""
    env = {key: value for key, value in os.environ.items() if key != "KITSUNE_OUTPUT_DIR"}
    return patch.dict(os.environ, {**env, **settings}, clear=True)


class _Session:
    def __init__(self, roots=(), capable=True, error=None, delay=0):
        self.roots, self.capable, self.error, self.delay = list(roots), capable, error, delay
        self.requests = 0

    def check_client_capability(self, capability):
        return self.capable and capability.roots is not None

    async def list_roots(self):
        self.requests += 1
        await anyio.sleep(self.delay)
        if self.error:
            raise self.error
        return ListRootsResult(roots=[Root(uri=uri) for uri in self.roots])


class _Context:
    def __init__(self, session=None):
        self._session = session

    @property
    def session(self):
        if self._session is None:
            raise ValueError("Context is not available outside of a request")
        return self._session


def _first_root(context):
    return asyncio.run(playwright_tools.first_client_root(context))


class PathTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cwd = Path(directory.name) / "server"
        cwd = patch.object(Path, "cwd", return_value=self.cwd)
        cwd.start()
        self.addCleanup(cwd.stop)

    def with_root(self, root):
        token = playwright_tools.client_root.set(root)
        self.addCleanup(playwright_tools.client_root.reset, token)

    def test_without_root_or_output_dir_names_resolve_against_the_server_cwd(self):
        with _environment():
            self.assertEqual(playwright_tools._path("snap.txt"), self.cwd / "snap.txt")

    def test_the_client_root_replaces_the_server_cwd(self):
        self.with_root(Path("/work/project"))
        with _environment():
            self.assertEqual(playwright_tools._path("out/snap.txt"), Path("/work/project/out/snap.txt"))

    def test_output_dir_wins_over_the_client_root(self):
        self.with_root(Path("/work/project"))
        with _environment(KITSUNE_OUTPUT_DIR="/srv/out"):
            self.assertEqual(playwright_tools._path("snap.txt"), Path("/srv/out/snap.txt"))
        with _environment(KITSUNE_OUTPUT_DIR="relative/out"):
            self.assertEqual(playwright_tools._path("snap.txt"), self.cwd / "relative" / "out" / "snap.txt")
        with _environment(KITSUNE_OUTPUT_DIR="~/out"):
            self.assertEqual(playwright_tools._path("snap.txt"), Path("~/out/snap.txt").expanduser())

    def test_absolute_and_home_names_are_kept(self):
        self.with_root(Path("/work/project"))
        with _environment(KITSUNE_OUTPUT_DIR="/srv/out"):
            self.assertEqual(playwright_tools._path("/tmp/snap.txt"), Path("/tmp/snap.txt"))
            self.assertEqual(playwright_tools._path("~/snap.txt"), Path("~/snap.txt").expanduser())


class FirstClientRootTests(unittest.TestCase):
    def setUp(self):
        env = _environment()
        env.start()
        self.addCleanup(env.stop)

    def test_the_first_file_root_is_used(self):
        session = _Session(["file://host/share", "file:///work/my%20project", "file:///other"])
        self.assertEqual(_first_root(_Context(session)), Path("/work/my project"))

    def test_no_request_no_capability_or_no_file_root_gives_none(self):
        self.assertIsNone(_first_root(_Context()))
        session = _Session(["file:///work"], capable=False)
        self.assertIsNone(_first_root(_Context(session)))
        self.assertEqual(session.requests, 0)
        self.assertIsNone(_first_root(_Context(_Session([]))))
        self.assertIsNone(_first_root(_Context(_Session(["file://host/share"]))))
        self.assertEqual(_first_root(_Context(_Session(["file://localhost/work"]))), Path("/work"))

    def test_a_failing_or_silent_client_gives_none(self):
        error = McpError(ErrorData(code=-32601, message="List roots not supported"))
        self.assertIsNone(_first_root(_Context(_Session(["file:///work"], error=error))))
        with patch.object(playwright_tools, "ROOTS_TIMEOUT_SECONDS", 0.05):
            self.assertIsNone(_first_root(_Context(_Session(["file:///work"], delay=5))))

    def test_output_dir_skips_the_roots_request(self):
        session = _Session(["file:///work"])
        with _environment(KITSUNE_OUTPUT_DIR="/srv/out"):
            self.assertIsNone(_first_root(_Context(session)))
        self.assertEqual(session.requests, 0)


class ClientRootsTests(unittest.TestCase):
    """Core tool calls through a connected MCP client session that lists roots."""

    def setUp(self):
        # The lowlevel server logs every request at INFO.
        logger = logging.getLogger("mcp.server.lowlevel.server")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)
        env = _environment()
        env.start()
        self.addCleanup(env.stop)
        playwright_tools._current_tab = None
        post = patch.object(server, "_post", return_value={"snapshot": '- button "OK" [ref=e1]', "url": "https://a/"})
        post.start()
        self.addCleanup(post.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.roots_requests = 0

    def call(self, name, arguments, roots=True):
        async def list_roots(context):
            self.roots_requests += 1
            return ListRootsResult(roots=[Root(uri=self.root.as_uri())])

        async def run():
            async with create_connected_server_and_client_session(
                    server.mcp, list_roots_callback=list_roots if roots else None) as client:
                return await client.call_tool(name, arguments)

        result = anyio.run(run)
        self.assertFalse(result.isError, result.content)
        return json.loads(result.content[0].text)

    def test_relative_file_names_are_written_under_the_client_root(self):
        result = self.call("browser_snapshot", {"filename": "out/snap.txt", "boxes": True})
        self.assertEqual(result["saved_to"], str(self.root / "out" / "snap.txt"))
        self.assertEqual((self.root / "out" / "snap.txt").read_text(encoding="utf-8"), '- button "OK" [ref=e1]')
        self.assertEqual(self.roots_requests, 1)
        self.assertIsNone(playwright_tools.client_root.get())

    def test_calls_without_a_filename_do_not_ask_for_roots(self):
        self.call("browser_snapshot", {})
        self.assertEqual(self.roots_requests, 0)

    def test_a_client_without_roots_falls_back_to_the_server_cwd(self):
        with patch.object(Path, "cwd", return_value=self.root / "cwd"):
            result = self.call("browser_snapshot", {"filename": "snap.txt"}, roots=False)
        self.assertEqual(result["saved_to"], str(self.root / "cwd" / "snap.txt"))


if __name__ == "__main__":
    unittest.main()
