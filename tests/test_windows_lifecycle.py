"""Native Windows lifecycle checks against owned processes and temporary homes."""

import io
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_bridge_security import Bridge, _bridge_env, _free_port
from kitsune import bridge_control as control
from kitsune import private_files


@unittest.skipUnless(sys.platform == "win32", "native Windows lifecycle")
class WindowsLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name
        self.env = _bridge_env(self.home, _free_port(), _free_port())
        for name, value in (("DATA_DIR", self.home),
                            ("LOCK_FILE", str(Path(self.home, "bridge.lock"))),
                            ("PID_FILE", str(Path(self.home, "bridge.pid")))):
            patched = patch.object(control, name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def start_bridge(self):
        bridge = Bridge(self.home).start()
        self.addCleanup(bridge.stop)
        return bridge

    def test_pid_remains_readable_while_daemon_holds_lock(self):
        bridge = self.start_bridge()
        self.assertEqual(Path(control.PID_FILE).read_text().strip(), str(bridge.process.pid))
        self.assertEqual(control.daemon_pid(), bridge.process.pid)

    def test_crash_releases_pid_lock_and_allows_restart(self):
        first = self.start_bridge()
        first.process.kill()
        first.process.wait(timeout=10)
        self.assertIsNone(control.daemon_pid())
        second = self.start_bridge()
        self.assertEqual(control.daemon_pid(), second.process.pid)

    def test_verified_pid_fallback_stops_only_the_owned_bridge(self):
        bridge = self.start_bridge()
        self.assertTrue(control._terminate_daemon())
        bridge.process.wait(timeout=10)
        self.assertIsNone(control.daemon_pid())

    def test_locked_file_cannot_authorize_stopping_an_unrelated_process(self):
        source = (
            "import os,sys,kitsune.file_lock as locks\n"
            "f=open(sys.argv[1], 'w+')\n"
            "locks.lock_exclusive(f)\n"
            "f.write(str(os.getpid()));f.flush()\n"
            "print('ready',flush=True)\n"
            "sys.stdin.read()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", source, control.PID_FILE], env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            executable=sys._base_executable,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.assertEqual(control.daemon_pid(), process.pid)
            self.assertFalse(control._terminate_daemon())
            self.assertIsNone(process.poll())
        finally:
            process.communicate(input="", timeout=10)

    def test_blocked_lifecycle_lock_does_not_extend_file_or_hide_its_contents(self):
        with control.lifecycle_lock():
            self.assertIsNone(control.try_lifecycle_lock())
            self.assertEqual(Path(control.LOCK_FILE).read_bytes(), b"")
        lock = control.try_lifecycle_lock()
        self.assertIsNotNone(lock)
        lock.close()

    def test_two_independent_homes_can_run_simultaneously(self):
        first = self.start_bridge()
        with tempfile.TemporaryDirectory() as other_home:
            second = Bridge(other_home).start()
            try:
                self.assertEqual(first.status_code(), 200)
                self.assertEqual(second.status_code(), 200)
            finally:
                second.stop()

    def test_token_and_home_acl_exclude_unrelated_local_users(self):
        import win32api
        import win32con
        import win32security

        # As the MCP server's lifecycle lock does before the log file exists in it.
        private_files.ensure_directory(self.home)
        bridge = Bridge(self.home, token=None).start()
        self.addCleanup(bridge.stop)
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        expected = {win32security.ConvertSidToStringSid(sid) for sid in (
            owner, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid),
            win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid))}
        for path in (Path(self.home), Path(self.home, "token")):
            descriptor = win32security.GetNamedSecurityInfo(
                str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
            self.assertTrue(descriptor.GetSecurityDescriptorControl()[0]
                            & win32security.SE_DACL_PROTECTED)
            acl = descriptor.GetSecurityDescriptorDacl()
            actual = {win32security.ConvertSidToStringSid(acl.GetAce(i)[2])
                      for i in range(acl.GetAceCount())}
            self.assertEqual(actual, expected)

    def dacl(self, path):
        import win32security

        descriptor = win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
        return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
            descriptor, win32security.SDDL_REVISION_1, win32security.DACL_SECURITY_INFORMATION)

    def test_a_folder_holding_other_files_keeps_its_acl_and_the_token_stays_private(self):
        Path(self.home, "notes.txt").write_text("not Kitsune's", encoding="utf-8")
        before = self.dacl(self.home)
        with patch.object(private_files, "_warned", set()), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            private_files.ensure_directory(self.home)
            private_files.ensure_directory(self.home)
        self.assertEqual(self.dacl(self.home), before)
        self.assertFalse(private_files._has_private_acl(self.home))
        self.assertEqual(err.getvalue().count("permissions were left unchanged"), 1, err.getvalue())
        bridge = Bridge(self.home, token=None).start()
        self.addCleanup(bridge.stop)
        self.assertEqual(self.dacl(self.home), before)
        self.assertTrue(private_files._has_private_acl(str(Path(self.home, "token"))))

    def test_a_directory_the_bridge_made_private_is_reused_without_a_warning(self):
        home = str(Path(self.home, "new", "kitsune"))
        private_files.ensure_directory(home)
        self.assertTrue(private_files._has_private_acl(home))
        Path(home, "bridge.log").write_text("", encoding="utf-8")
        with patch.object(private_files, "_warned", set()), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            private_files.ensure_directory(home)
        self.assertEqual(err.getvalue(), "")
        self.assertTrue(private_files._has_private_acl(home))

    def test_acl_failure_is_not_silently_ignored(self):
        with patch.object(private_files, "_windows_acl", side_effect=OSError("ACL unavailable")):
            with self.assertRaisesRegex(OSError, "ACL unavailable"):
                private_files.ensure_directory(self.home)

    def test_signal_fallback_restores_handlers_even_after_startup_error(self):
        import signal

        loop = Mock()
        loop.add_signal_handler.side_effect = NotImplementedError
        callback = Mock()
        previous = {signal.SIGTERM: Mock(), signal.SIGINT: Mock()}
        with patch.object(signal, "getsignal", side_effect=previous.get), \
                patch.object(signal, "signal") as install:
            with self.assertRaisesRegex(RuntimeError, "startup"):
                with control.shutdown_signals(loop, callback):
                    handler = install.call_args_list[0].args[1]
                    handler(signal.SIGTERM, None)
                    loop.call_soon_threadsafe.assert_called_once_with(callback)
                    raise RuntimeError("startup")
            self.assertEqual(install.call_args_list[-2].args, (signal.SIGINT, previous[signal.SIGINT]))
            self.assertEqual(install.call_args_list[-1].args, (signal.SIGTERM, previous[signal.SIGTERM]))

    def test_changed_pid_identity_prevents_fallback_termination(self):
        import psutil

        process = Mock()
        process.cmdline.return_value = [sys.executable, "-m", "kitsune.bridge"]
        with patch.object(psutil, "Process", return_value=process), \
                patch.object(control, "_locked_pid", side_effect=[12345, 54321]):
            self.assertFalse(control._terminate_daemon())
        process.terminate.assert_not_called()

    def test_bridge_words_in_a_script_argument_do_not_authorize_termination(self):
        import psutil

        process = Mock()
        process.cmdline.return_value = [sys.executable, "-c", "pass", "-m", "kitsune.bridge"]
        with patch.object(psutil, "Process", return_value=process), \
                patch.object(control, "_locked_pid", return_value=12345):
            self.assertFalse(control._terminate_daemon())
        process.terminate.assert_not_called()

    def test_startup_logs_work_with_non_unicode_parent_encoding(self):
        bridge = Bridge(self.home)
        bridge.env["PYTHONIOENCODING"] = "ascii"
        self.addCleanup(bridge.stop)
        bridge.start()
        self.assertEqual(bridge.status_code(), 200)

    def test_second_home_cannot_take_over_an_existing_http_listener(self):
        first = self.start_bridge()
        with tempfile.TemporaryDirectory() as other_home:
            second = Bridge(other_home)
            second.http_port = first.http_port
            second.env["KITSUNE_HTTP_PORT"] = str(first.http_port)
            self.addCleanup(second.stop)
            second.spawn()
            self.assertNotEqual(second.process.wait(timeout=15), 0)
            self.assertIsNone(first.process.poll())
            self.assertEqual(first.status_code(), 200)

    def test_loopback_listeners_cannot_be_taken_over_with_address_reuse(self):
        bridge = self.start_bridge()
        for port in (bridge.http_port, bridge.ws_port):
            with self.subTest(port=port), socket.socket() as intruder:
                intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with self.assertRaises(OSError):
                    intruder.bind(("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
