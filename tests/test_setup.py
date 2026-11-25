"""Client setup in disposable homes; no installed client, bridge or network needed."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tomlkit
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from kitsune import cli, setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.uvx = str(self.home / "bin with spaces" / ("uvx.exe" if os.name == "nt" else "uvx"))
        self.binaries = {"uvx": self.uvx}
        for p in (patch.object(Path, "home", return_value=self.home),
                  patch.object(setup.shutil, "which", side_effect=self.binaries.get),
                  patch.dict(os.environ, dict.fromkeys(
                      ["CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "APPDATA"], ""))):
            p.start()
            self.addCleanup(p.stop)

    def run_setup(self, *args, keys=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            if keys is None:
                code = cli.main(["setup", *args])
            else:
                with (create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()),
                      patch.object(sys.stdin, "isatty", return_value=True),
                      patch.object(out, "isatty", return_value=True)):
                    pipe.send_text(keys)
                    code = cli.main(["setup", *args])
        return code, out.getvalue(), err.getvalue()

    def test_list_is_read_only_and_previews_detection(self):
        (self.home / ".codex").mkdir()
        self.binaries["pi"] = "pi"
        code, out, err = self.run_setup("--list")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 6)
        for name in ("Claude Code", "Codex CLI", "Pi", "Claude Desktop", "Cursor", "OpenCode"):
            self.assertIn(name, out)
        for label in ("Claude Code (not detected)", "Codex CLI (detected)", "Pi (detected)"):
            self.assertIn(label, out)
        self.assertFalse(any(path.is_file() for path in self.home.rglob("*")))

    def test_auto_writes_only_detected_clients(self):
        self.binaries["codex"] = "codex"
        (self.home / ".cursor").mkdir()
        code, out, err = self.run_setup("--auto")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Codex CLI: Configured", out)
        for c in setup.clients():
            if c.name == "Codex CLI":
                entry = tomlkit.parse(c.config.read_text())["mcp_servers"]["kitsune"]
            elif c.name == "Cursor":
                entry = json.loads(c.config.read_bytes())["mcpServers"]["kitsune"]
            else:
                self.assertFalse(c.config and c.config.exists())
                continue
            self.assertEqual(entry, {"command": self.uvx, "args": ["kitsune-browser-mcp@latest"]})

    def test_space_changes_defaults_and_enter_applies(self):
        self.binaries["claude"] = "claude"  # initially checked; turn it off and Codex on
        code, _, err = self.run_setup(keys=" \x1b[B \r")
        self.assertEqual((code, err), (0, ""))
        self.assertFalse((self.home / ".claude.json").exists())
        self.assertTrue((self.home / ".codex/config.toml").is_file())

    def test_cancel_non_tty_and_missing_uvx_do_not_write(self):
        self.binaries["claude"] = "claude"
        self.assertEqual(self.run_setup(keys="\x03")[0], 130)
        self.assertEqual(self.run_setup()[0], 2)
        del self.binaries["uvx"]
        self.assertIn("uvx not found", self.run_setup("--auto")[2])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_migration_preserves_settings_and_skips_a_second_write(self):
        with patch.object(sys, "platform", "darwin"):
            clients = setup.clients()
        for c in clients:
            with self.subTest(client=c.name):
                c.config.parent.mkdir(parents=True, exist_ok=True)
                old = ["--from", "kitsune-browser-mcp==0.1", "kitsune"]
                key = {"OpenCode": "mcp", "Codex CLI": "mcp_servers"}.get(c.name, "mcpServers")
                entry = ({"type": "local", "command": ["uvx", *old], "enabled": False} if c.name == "OpenCode"
                         else {"command": "uvx", "args": old, "env": {"KITSUNE_SCRIPTING": "0"}})
                data = {"setting": "keep", key: {"other": {"command": "other"}, "kitsune": entry}}
                codex = c.name == "Codex CLI"
                c.config.write_text(("# keep my comment\n" + tomlkit.dumps(data)) if codex else json.dumps(data),
                                    encoding="utf-8")
                self.assertEqual(setup.register(c, self.uvx), "Configured")
                text = c.config.read_text(encoding="utf-8")
                if codex:
                    self.assertIn("# keep my comment", text)
                launch = ({"command": [self.uvx, "kitsune-browser-mcp@latest"]} if c.name == "OpenCode"
                          else {"command": self.uvx, "args": ["kitsune-browser-mcp@latest"]})
                entry.update(launch)
                self.assertEqual(tomlkit.parse(text) if codex else json.loads(text), data)
                with patch.object(setup, "_save", side_effect=AssertionError("redundant write")):
                    self.assertEqual(setup.register(c, self.uvx), "Already configured")

    def test_kitsune_under_another_name_is_updated_not_added_twice(self):
        with patch.object(sys, "platform", "darwin"):
            clients = setup.clients()

        def entry(opencode, command, *args):
            return {"type": "local", "command": [command, *args]} if opencode else {"command": command, "args": list(args)}

        for c in clients:
            opencode, codex = c.name == "OpenCode", c.name == "Codex CLI"
            key = {"OpenCode": "mcp", "Codex CLI": "mcp_servers"}.get(c.name, "mcpServers")
            standard = entry(opencode, "uvx", "--from", "kitsune-browser-mcp", "kitsune")
            checkout = entry(opencode, "/home/me/.local/bin/uv", "run", "--directory", "/src/kitsune", "kitsune")
            unrelated = entry(opencode, "uvx", "other-mcp")
            for servers, outcome in (
                    ({"kitsune-browser": standard, "tool": unrelated}, "Configured as kitsune-browser"),
                    ({"kitsune-browser": checkout}, "custom launcher"),
                    ({"a": standard, "b": standard}, "registered more than once (a, b)"),
                    ({"tool": unrelated}, "Configured")):
                with self.subTest(client=c.name, servers=list(servers)):
                    c.config.parent.mkdir(parents=True, exist_ok=True)
                    data = {key: json.loads(json.dumps(servers))}
                    before = tomlkit.dumps(data) if codex else json.dumps(data)
                    c.config.write_text(before, encoding="utf-8")
                    if outcome.startswith("Configured"):
                        self.assertEqual(setup.register(c, self.uvx), outcome)
                        text = c.config.read_text(encoding="utf-8")
                        written = (tomlkit.parse(text) if codex else json.loads(text))[key]
                        name = outcome.removeprefix("Configured as ") if " as " in outcome else "kitsune"
                        self.assertEqual(sorted(written), sorted({*servers, name}), "one Kitsune entry")
                        launch = written[name]["command"] if opencode else [written[name]["command"],
                                                                             *written[name]["args"]]
                        self.assertEqual(list(launch), [self.uvx, "kitsune-browser-mcp@latest"])
                        self.assertEqual(json.loads(json.dumps(written["tool"])) if "tool" in written else None,
                                         unrelated if "tool" in servers else None)
                    else:
                        with self.assertRaisesRegex(ValueError, outcome.replace("(", r"\(").replace(")", r"\)")):
                            setup.register(c, self.uvx)
                        self.assertEqual(c.config.read_text(encoding="utf-8"), before, "left unchanged")

    def test_bad_configs_do_not_block_other_clients(self):
        clients = {c.name: c.config for c in setup.clients()}
        bad = {
            clients["Claude Code"]: '{"mcpServers":{"kitsune":{"url":"https://example.com/secret"}}}',
            clients["Codex CLI"]: "[mcp_servers\n",
            clients["Pi"]: '{"mcpServers":[]}',
            clients["OpenCode"].with_suffix(".jsonc"): '{ // keep comment\n "mcp": {} }',
        }
        for path, text in bad.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        code, out, _ = self.run_setup("--auto")
        self.assertEqual(code, 1)
        self.assertNotIn("Restart", out)

        self.binaries["cursor"] = "cursor"
        code, out, err = self.run_setup("--auto")
        self.assertIn("Restart", out)
        self.assertEqual(code, 1)
        self.assertTrue(clients["Cursor"].is_file())
        self.assertFalse(clients["OpenCode"].with_suffix(".json").exists())
        self.assertNotIn("example.com/secret", err)
        for path, text in bad.items():
            self.assertIn(str(path), err)
            self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_user_config_overrides_and_platform_paths(self):
        variables = {name: str(self.home / name) for name in
                     ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "APPDATA", "XDG_CONFIG_HOME")}
        with patch.dict(os.environ, variables), patch.object(sys, "platform", "win32"):
            paths = {c.name: c.config.relative_to(self.home).as_posix() for c in setup.clients()}
        self.assertEqual(paths, {"Codex CLI": "CODEX_HOME/config.toml", "Claude Code": "CLAUDE_CONFIG_DIR/.claude.json",
                                 "Pi": "PI_CODING_AGENT_DIR/mcp.json", "Cursor": ".cursor/mcp.json",
                                 "Claude Desktop": "APPDATA/Claude/claude_desktop_config.json",
                                 "OpenCode": "XDG_CONFIG_HOME/opencode/opencode.json"})
        with patch.object(sys, "platform", "linux"):
            desktop = next(c for c in setup.clients() if c.name == "Claude Desktop")
            self.assertIsNone(desktop.config)
            self.assertFalse(desktop.detected)

    def test_no_arguments_runs_the_stdio_server(self):
        serve = Mock()
        with patch.dict(sys.modules, {"kitsune.server": SimpleNamespace(main=serve)}), patch.object(setup, "run") as run:
            cli.main([])
        serve.assert_called_once_with()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
