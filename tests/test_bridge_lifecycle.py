"""Bridge build matching, safe replacement, and CLI tests (no sockets/processes)."""
import asyncio
from contextlib import nullcontext
from email.message import Message
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import ANY, AsyncMock, Mock, mock_open, patch

import httpx
from kitsune import bridge, bridge_client, server, tools
import kitsune.bridge_control as control
import kitsune.version as version

# Popen of a spawned bridge that has not exited.
RUNNING = {"return_value.poll.return_value": None}


class EnsureBridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"KITSUNE_NO_BRIDGE_RESTART": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # A started bridge makes the next command wait for its extension (#258); keep that to this test.
        wait = patch.object(server._client, "_extension_wait_until", 0.0)
        wait.start()
        self.addCleanup(wait.stop)

    def test_matching_build_does_not_restart(self):
        with patch.object(control, "status", return_value={"build": bridge_client.BRIDGE_BUILD}), \
                patch.object(control, "lifecycle_lock") as lock, patch.object(control, "stop") as stop:
            self.assertIsNone(server._client.ensure_bridge())
            lock.assert_not_called()
            stop.assert_not_called()

    def test_mismatch_and_old_bridge_restart_then_wait_for_matching_build(self):
        for old in ({"build": "old"}, {"bridge_version": "2.1.0"}):
            with self.subTest(old=old), \
                    patch.object(control, "status", side_effect=[old, old, None, {"build": bridge_client.BRIDGE_BUILD}]), \
                    patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
                    patch.object(control, "stop", return_value=None) as stop, \
                    patch("builtins.open", mock_open()), \
                    patch.object(bridge_client.subprocess, "Popen", **RUNNING) as spawn, patch.object(bridge_client.time, "sleep"):
                self.assertIsNone(server._client.ensure_bridge())
                stop.assert_called_once_with()
                self.assertEqual(spawn.call_count, 1)
                self.assertEqual(spawn.call_args.args[0], [bridge_client.sys.executable, "-m", "kitsune.bridge"])

    def test_opt_out_keeps_old_or_mismatched_bridge(self):
        for old in ({}, {"build": "old"}):
            with self.subTest(old=old), patch.dict(os.environ, {"KITSUNE_NO_BRIDGE_RESTART": "1"}), \
                    patch.object(control, "status", return_value=old), patch.object(control, "stop") as stop:
                self.assertIsNone(server._client.ensure_bridge())
                stop.assert_not_called()

    def test_recheck_under_lock_reuses_replacement_from_another_client(self):
        with patch.object(control, "status", side_effect=[{}, {"build": bridge_client.BRIDGE_BUILD}]), \
                patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
                patch.object(control, "stop") as stop:
            self.assertIsNone(server._client.ensure_bridge())
            stop.assert_not_called()

    def test_stop_failure_does_not_spawn(self):
        with patch.object(control, "status", return_value={}), \
                patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
                patch.object(control, "stop", return_value="cannot safely stop"), \
                patch.object(bridge_client.subprocess, "Popen", **RUNNING) as spawn:
            self.assertEqual(server._client.ensure_bridge(), bridge_client._bridge_unavailable("cannot safely stop"))
            spawn.assert_not_called()

    def test_no_bridge_starts_even_with_opt_out(self):
        with patch.dict(os.environ, {"KITSUNE_NO_BRIDGE_RESTART": "1"}), \
                patch.object(control, "status", side_effect=[None, None, {"build": bridge_client.BRIDGE_BUILD}]), \
                patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
                patch("builtins.open", mock_open()), patch.object(bridge_client.subprocess, "Popen", **RUNNING) as spawn:
            self.assertIsNone(server._client.ensure_bridge())
            spawn.assert_called_once()

    def test_start_wait_rejects_wrong_build(self):
        with patch.object(control, "status", side_effect=[None, None, {"build": "wrong"}]), \
                patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
                patch("builtins.open", mock_open()), patch.object(bridge_client.subprocess, "Popen", **RUNNING), \
                patch.object(bridge_client.time, "monotonic", side_effect=[0, 0, 16]), patch.object(bridge_client.time, "sleep"), \
                patch.object(control, "port_in_use") as port_in_use:
            error = server._client.ensure_bridge() or {}
            self.assertIn("did not start", error.get("error", ""))
            self.assertEqual(error.get("code"), "bridge_unavailable")
            port_in_use.assert_not_called()  # a bridge of another build answers on the port


class BridgeControlTests(unittest.TestCase):
    def test_graceful_stop_uses_token_and_does_not_signal(self):
        with patch.object(control, "status", return_value={"build": "old"}), \
                patch.object(control, "headers", return_value={"Authorization": "Bearer secret"}), \
                patch.object(control.httpx, "post", return_value=Mock(status_code=200)) as post, \
                patch.object(control, "_wait_stopped", return_value=True), \
                patch.object(control, "_terminate_daemon") as terminate:
            self.assertIsNone(control.stop())
            post.assert_called_once_with(control.BRIDGE + "/api/shutdown", headers={"Authorization": "Bearer secret"}, timeout=2)
            terminate.assert_not_called()

    def test_missing_shutdown_endpoint_falls_back_to_verified_pid(self):
        with patch.object(control, "status", return_value={}), \
                patch.object(control.httpx, "post", return_value=Mock(status_code=404)), \
                patch.object(control, "_terminate_daemon", return_value=True) as terminate, \
                patch.object(control, "_wait_stopped", return_value=True):
            self.assertIsNone(control.stop())
            terminate.assert_called_once()

    def test_legacy_bridge_without_pid_gives_actionable_error(self):
        with patch.object(control, "status", return_value={}), \
                patch.object(control.httpx, "post", return_value=Mock(status_code=404)), \
                patch.object(control, "_locked_pid", return_value=None), patch.object(control.os, "kill") as kill:
            self.assertIn("manually", control.stop() or "")
            kill.assert_not_called()

    def test_pid_fallback_checks_command(self):
        for command, expected in [("/usr/bin/python -m kitsune.bridge", True),
                                  ("/venv/bin/python /venv/bin/kitsune-bridge", True),
                                  ("unrelated-daemon", False)]:
            with self.subTest(command=command), patch.object(control, "_locked_pid", return_value=12345), \
                    patch.object(control.subprocess, "run", return_value=Mock(stdout=command)), \
                    patch.object(control.os, "kill") as kill:
                self.assertEqual(control._terminate_daemon(), expected)
                if expected:
                    kill.assert_called_once_with(12345, signal.SIGTERM)
                else:
                    kill.assert_not_called()

    def test_stale_pid_file_is_not_trusted(self):
        with patch("builtins.open", mock_open(read_data="12345")), \
                patch.object(control.fcntl, "flock"), patch.object(control.os, "kill") as kill:
            self.assertIsNone(control._locked_pid())
            kill.assert_not_called()

    def test_wait_requires_http_and_pid_lock_to_be_gone(self):
        with patch.object(control, "status", return_value=None), \
                patch.object(control, "_locked_pid", side_effect=[12345, None]), \
                patch.object(control.time, "sleep") as sleep:
            self.assertTrue(control._wait_stopped())
            sleep.assert_called_once()

    def test_wait_retries_connection_reset_during_shutdown(self):
        with patch.object(control, "status", side_effect=[httpx.ReadError("closing"), None]), \
                patch.object(control, "_locked_pid", return_value=None), \
                patch.object(control.time, "sleep") as sleep:
            self.assertTrue(control._wait_stopped())
            sleep.assert_called_once()

    def test_unresponsive_bridge_still_uses_verified_pid_fallback(self):
        with patch.object(control, "status", side_effect=httpx.ReadTimeout("hung")), \
                patch.object(control.httpx, "post", side_effect=httpx.ReadTimeout("hung")), \
                patch.object(control, "_terminate_daemon", return_value=True) as terminate, \
                patch.object(control, "_wait_stopped", return_value=True):
            self.assertIsNone(control.stop())
            terminate.assert_called_once()

    def test_status_reads_auth_and_rejects_http_errors(self):
        with patch.object(control, "headers", return_value={"Authorization": "Bearer token"}), \
                patch.object(control.httpx, "get", return_value=httpx.Response(
                    401, request=httpx.Request("GET", control.BRIDGE))) as get:
            with self.assertRaises(httpx.HTTPStatusError):
                control.status()
            self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer token"})


class BridgeCliAndStatusTests(unittest.TestCase):
    def test_cli_status_does_not_start_bridge(self):
        for status in (None, {"build": "example", "status": "running"}):
            with self.subTest(status=status), patch.object(control, "status", return_value=status), \
                    patch.object(bridge.asyncio, "run") as run, patch("sys.stdout", new_callable=io.StringIO) as out:
                bridge.run(["status"])
                self.assertEqual(json.loads(out.getvalue()), status if status is not None else {"status": "stopped"})
                run.assert_not_called()

    def test_cli_stop_is_serialized_and_does_not_start(self):
        with patch.object(control, "lifecycle_lock", return_value=nullcontext()) as lock, \
                patch.object(control, "stop", return_value=None) as stop, \
                patch.object(bridge.asyncio, "run") as run, patch("sys.stdout", new_callable=io.StringIO):
            bridge.run(["stop"])
            lock.assert_called_once()
            stop.assert_called_once()
            run.assert_not_called()

    def test_cli_stop_failure_has_nonzero_exit(self):
        with patch.object(control, "lifecycle_lock", return_value=nullcontext()), \
                patch.object(control, "stop", return_value="cannot stop"), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as raised:
                bridge.run(["stop"])
            self.assertEqual(raised.exception.code, 1)

    def test_cli_rejects_unknown_commands_and_extra_arguments(self):
        for args in (["restart"], ["status", "extra"], ["stop", "extra"]):
            with self.subTest(args=args), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    bridge.run(args)
                self.assertEqual(raised.exception.code, 2)

    def test_plain_cli_and_explicit_start_keep_starting_daemon(self):
        for args in ([], ["start"]):
            with self.subTest(args=args), patch.object(bridge.os, "open", return_value=42), \
                    patch.object(bridge.os, "fdopen", mock_open()), patch.object(bridge.fcntl, "flock"), \
                    patch.object(bridge, "main", new=Mock(return_value="coroutine")), \
                    patch.object(bridge.asyncio, "run") as run:
                bridge.run(args)
                run.assert_called_once_with("coroutine")

    def test_duplicate_daemon_is_rejected_before_start(self):
        with patch.object(bridge.os, "open", return_value=42), \
                patch.object(bridge.os, "fdopen", mock_open()), \
                patch.object(bridge.fcntl, "flock", side_effect=BlockingIOError), \
                patch.object(bridge.asyncio, "run") as run, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as raised:
                bridge.run([])
            self.assertEqual(raised.exception.code, 1)
            run.assert_not_called()

    def test_main_closes_both_servers_on_graceful_shutdown(self):
        async def exercise():
            loop = asyncio.get_running_loop()
            event = asyncio.Event()
            event.set()
            websocket_server = Mock(wait_closed=AsyncMock())
            http_server = Mock()
            with patch.object(loop, "add_signal_handler"), patch.object(loop, "remove_signal_handler"), \
                    patch.object(bridge.asyncio, "Event", return_value=event), \
                    patch.object(bridge, "ws_serve", new=AsyncMock(return_value=websocket_server)), \
                    patch.object(bridge, "BridgeHTTPServer", return_value=http_server), \
                    patch.object(bridge, "Thread"), patch("builtins.print"), \
                    patch.object(bridge.asyncio, "to_thread", new=AsyncMock()) as to_thread:
                await bridge.main()
                to_thread.assert_awaited_once_with(http_server.shutdown)
                http_server.server_close.assert_called_once()
                websocket_server.close.assert_called_once()
                websocket_server.wait_closed.assert_awaited_once()
        asyncio.run(exercise())

    def test_shutdown_endpoint_is_authenticated(self):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.path = "/api/shutdown"
        handler.command = "POST"
        handler.headers = Message()
        handler.headers["Host"] = f"127.0.0.1:{bridge.HTTP_PORT}"
        handler.send_json = Mock()
        handler.handle_shutdown = Mock()
        handler.do_POST()
        handler.send_json.assert_called_once_with(401, ANY)
        handler.handle_shutdown.assert_not_called()
        handler.headers["Authorization"] = "Bearer " + bridge.AUTH_TOKEN
        handler.do_POST()
        handler.handle_shutdown.assert_called_once()

    def test_shutdown_schedules_graceful_cleanup(self):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.send_json = Mock()
        with patch.object(bridge, "loop") as loop, patch.object(bridge, "shutdown_event") as event:
            handler.handle_shutdown()
            handler.send_json.assert_called_once_with(200, {"status": "stopping"})
            loop.call_soon_threadsafe.assert_called_once_with(event.set)

    def test_status_and_browser_status_report_shared_build(self):
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.send_json = Mock()
        handler.handle_status()
        data = handler.send_json.call_args.args[1]
        self.assertEqual(data["build"], bridge_client.BRIDGE_BUILD)
        self.assertEqual(data["bridge_version"], version.PACKAGE_VERSION)
        self.assertIn("extension_connected", data)
        with patch.object(tools, "_get", return_value=data):
            self.assertEqual(server.browser_status()["build"], bridge_client.BRIDGE_BUILD)


class BridgeIdleShutdownTests(unittest.TestCase):
    """Opt-in idle shutdown: configuration, activity accounting and the watcher."""

    def setUp(self):
        self.activity = bridge._ClientActivity()
        for name, value in (("_client_activity", self.activity), ("_idle_shutdown_lock", None),
                            ("IDLE_POLL_SECONDS", 0.01), ("pending_commands", {}), ("_keep_alive_tasks", {})):
            patcher = patch.object(bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.lock = Mock(name="lifecycle-lock")
        try_lock = patch.object(control, "try_lifecycle_lock", return_value=self.lock)
        self.try_lock = try_lock.start()
        self.addCleanup(try_lock.stop)

    def watch(self, idle_timeout, during=None, run_for=1.0):
        """Run the watcher; return seconds until shutdown, or None if it never fired."""
        async def exercise():
            bridge.shutdown_event = asyncio.Event()
            self.activity.touch()
            watcher = asyncio.create_task(bridge._idle_shutdown_watch(idle_timeout))
            started = asyncio.get_running_loop().time()
            if during is not None:
                await during()
            try:
                await asyncio.wait_for(bridge.shutdown_event.wait(), run_for)
            except asyncio.TimeoutError:
                watcher.cancel()
                return None
            await watcher
            return asyncio.get_running_loop().time() - started
        with patch.object(bridge, "shutdown_event", None), patch("builtins.print"):
            return asyncio.run(exercise())

    def test_idle_timeout_is_disabled_by_default_and_configurable(self):
        cases = [([], {}, 0), ([], {bridge.IDLE_TIMEOUT_ENV: ""}, 0), ([], {bridge.IDLE_TIMEOUT_ENV: "30"}, 30),
                 (["start", "--idle-timeout", "5"], {bridge.IDLE_TIMEOUT_ENV: "30"}, 5),
                 (["--idle-timeout", "0"], {bridge.IDLE_TIMEOUT_ENV: "30"}, 0)]
        for args, env, expected in cases:
            environ = {k: v for k, v in os.environ.items() if k != bridge.IDLE_TIMEOUT_ENV}
            with self.subTest(args=args, env=env), patch.dict(os.environ, {**environ, **env}, clear=True), \
                    patch.object(bridge.os, "open", return_value=42), \
                    patch.object(bridge.os, "fdopen", mock_open()), patch.object(bridge.fcntl, "flock"), \
                    patch.object(bridge, "main", new=Mock(return_value="coroutine")) as main, \
                    patch.object(bridge.asyncio, "run"):
                bridge.run(args)
                main.assert_called_once_with(expected)

    def test_invalid_idle_timeout_is_rejected_before_start(self):
        cases = [(["--idle-timeout", "soon"], {}), (["--idle-timeout", "-1"], {}),
                 (["--idle-timeout", "inf"], {}), ([], {bridge.IDLE_TIMEOUT_ENV: "nan"}),
                 (["stop", "--idle-timeout", "5"], {}), (["status", "--idle-timeout", "5"], {})]
        for args, env in cases:
            with self.subTest(args=args, env=env), patch.dict(os.environ, env), \
                    patch.object(bridge.asyncio, "run") as run, patch.object(control, "stop") as stop, \
                    patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    bridge.run(args)
                self.assertEqual(raised.exception.code, 2)
                run.assert_not_called()
                stop.assert_not_called()

    def test_activity_accounting_tracks_in_flight_and_last_request(self):
        with patch.object(bridge.time, "monotonic", side_effect=[0, 10, 20, 25, 40, 45]):
            activity = bridge._ClientActivity()        # t=0
            activity.begin()                           # t=10
            activity.begin()                           # t=20: a concurrent client
            activity.end()                             # t=25
            self.assertEqual(activity.in_flight(), 1)
            self.assertEqual(activity.idle_seconds(), 0)  # never idle while one is in flight
            activity.end()                             # t=40
            self.assertEqual(activity.in_flight(), 0)
            self.assertEqual(activity.idle_seconds(), 5)  # t=45, measured from the last request

    def test_http_server_counts_request_until_handler_thread_ends(self):
        server_ = bridge.BridgeHTTPServer.__new__(bridge.BridgeHTTPServer)
        with patch.object(bridge.ThreadingHTTPServer, "process_request") as process, \
                patch.object(bridge.ThreadingHTTPServer, "process_request_thread", side_effect=OSError) as thread:
            server_.process_request("request", "client")
            process.assert_called_once_with("request", "client")
            self.assertEqual(self.activity.in_flight(), 1)
            with self.assertRaises(OSError):
                server_.process_request_thread("request", "client")
            thread.assert_called_once_with("request", "client")
            self.assertEqual(self.activity.in_flight(), 0)
        with patch.object(bridge.ThreadingHTTPServer, "process_request", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                server_.process_request("request", "client")
            self.assertEqual(self.activity.in_flight(), 0)

    def test_idle_shutdown_fires_and_holds_lifecycle_lock(self):
        elapsed = self.watch(0.1)
        self.assertIsNotNone(elapsed)
        self.assertGreaterEqual(elapsed, 0.09)
        self.try_lock.assert_called_once_with()
        self.assertIs(bridge._idle_shutdown_lock, self.lock)

    def test_client_activity_resets_idle_timer(self):
        async def keep_calling():
            for _ in range(6):
                self.activity.begin()
                self.activity.end()
                await asyncio.sleep(0.05)
        elapsed = self.watch(0.15, keep_calling)
        self.assertIsNotNone(elapsed)
        self.assertGreaterEqual(elapsed, 0.25 + 0.14)  # last call at ~0.25s

    def test_in_flight_request_blocks_shutdown(self):
        self.activity.begin()
        self.assertIsNone(self.watch(0.05, run_for=0.3))
        self.try_lock.assert_not_called()
        self.activity.end()
        self.assertIsNotNone(self.watch(0.05))

    def test_pending_extension_command_or_keep_alive_blocks_shutdown(self):
        for name, value in (("pending_commands", {1: {}}), ("_keep_alive_tasks", {7: Mock(done=Mock(return_value=False))})):
            with self.subTest(name=name), patch.object(bridge, name, value):
                self.assertIsNone(self.watch(0.05, run_for=0.3))
                self.try_lock.assert_not_called()
        finished = {7: Mock(done=Mock(return_value=True))}
        with patch.object(bridge, "_keep_alive_tasks", finished):
            self.assertIsNotNone(self.watch(0.05))

    def test_client_holding_lifecycle_lock_defers_shutdown(self):
        self.try_lock.side_effect = [None, None, self.lock]
        self.assertIsNotNone(self.watch(0.05))
        self.assertEqual(self.try_lock.call_count, 3)
        self.assertIs(bridge._idle_shutdown_lock, self.lock)

    def test_idle_shutdown_drains_accepted_requests_before_closing(self):
        async def exercise():
            loop = asyncio.get_running_loop()
            event = asyncio.Event()
            event.set()
            closed_with = []
            websocket_server = Mock(wait_closed=AsyncMock())
            websocket_server.close.side_effect = lambda: closed_with.append(self.activity.in_flight())
            self.activity.begin()  # accepted just before the listener closed
            loop.call_later(0.2, self.activity.end)
            with patch.object(loop, "add_signal_handler"), patch.object(loop, "remove_signal_handler"), \
                    patch.object(bridge.asyncio, "Event", return_value=event), \
                    patch.object(bridge, "ws_serve", new=AsyncMock(return_value=websocket_server)), \
                    patch.object(bridge, "BridgeHTTPServer", return_value=Mock()), \
                    patch.object(bridge, "_idle_shutdown_lock", self.lock), \
                    patch.object(bridge, "Thread"), patch("builtins.print"), \
                    patch.object(bridge.asyncio, "to_thread", new=AsyncMock()):
                await bridge.main(idle_timeout=60)
            self.assertEqual(closed_with, [0])
        asyncio.run(exercise())

    def test_run_releases_lifecycle_lock_after_pid_lock(self):
        order = []
        pid_file = mock_open()
        pid_file.return_value.__exit__.side_effect = lambda *args: order.append("pid")
        self.lock.close.side_effect = lambda: order.append("lifecycle")
        with patch.object(bridge.os, "open", return_value=42), patch.object(bridge.os, "fdopen", pid_file), \
                patch.object(bridge.fcntl, "flock"), patch.object(bridge, "main", new=Mock()), \
                patch.object(bridge.asyncio, "run", side_effect=lambda _: setattr(bridge, "_idle_shutdown_lock", self.lock)):
            bridge.run(["--idle-timeout", "1"])
        self.assertEqual(order, ["pid", "lifecycle"])


class BridgeVersionTests(unittest.TestCase):
    def test_package_version_falls_back_to_pyproject(self):
        with patch.object(version, "version", side_effect=version.PackageNotFoundError), \
                patch.object(Path, "read_text", return_value='[project]\nversion = "1.2.3"\n[other]\n'):
            self.assertEqual(version.package_version(), "1.2.3")

    def test_build_changes_with_bridge_or_extension_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "src" / "kitsune"
            package.mkdir(parents=True)
            for name in ("bridge.py", "bridge_control.py", "capabilities.py", "task_modes.py", "version.py"):
                (package / name).write_text("original")
            extension = root / "extension"
            extension.mkdir()
            script = extension / "background.js"
            script.write_text("original")
            with patch.object(version, "PACKAGE_DIR", package), patch.object(version, "SOURCE_ROOT", root):
                first = version.bridge_build()
                self.assertEqual(first, version.bridge_build())
                (package / "bridge.py").write_text("updated")
                second = version.bridge_build()
                self.assertNotEqual(first, second)
                (package / "task_modes.py").write_text("updated modes")
                third = version.bridge_build()
                self.assertNotEqual(second, third)
                # The bridge enforces the capability tables, so a changed table replaces the daemon.
                (package / "capabilities.py").write_text("updated groups")
                self.assertNotEqual(third, version.bridge_build())
                third = version.bridge_build()
                script.write_text("updated")
                self.assertNotEqual(third, version.bridge_build())


if __name__ == "__main__":
    unittest.main()
