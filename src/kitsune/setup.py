"""Register the released MCP server in selected user-level client configs."""

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import questionary
import tomlkit

PACKAGE = "kitsune-browser-mcp"
LATEST = f"{PACKAGE}@latest"


@dataclass(frozen=True)
class Client:
    name: str
    config: Path | None
    detected: bool

    @property
    def label(self) -> str:
        if self.config is None:
            return f"{self.name} (macOS/Windows only)"
        return f"{self.name} ({'detected' if self.detected else 'not detected'})"


def clients() -> list[Client]:
    home = Path.home()

    def directory(var, default):
        return Path(os.environ.get(var) or default).expanduser()

    def client(name, path, binary=None, marker=None):
        detected = path is not None and (path.is_file() or (marker or path.parent).is_dir()
                                         or bool(binary and shutil.which(binary)))
        return Client(name, path, detected)

    claude = directory("CLAUDE_CONFIG_DIR", home)
    desktop = None
    if sys.platform == "darwin":
        desktop = home / "Library/Application Support/Claude/claude_desktop_config.json"
    elif sys.platform == "win32":
        desktop = directory("APPDATA", home / "AppData/Roaming") / "Claude/claude_desktop_config.json"
    opencode = directory("XDG_CONFIG_HOME", home / ".config") / "opencode/opencode.json"
    if opencode.with_suffix(".jsonc").is_file():
        opencode = opencode.with_suffix(".jsonc")

    return [
        client("Claude Code", claude / ".claude.json", "claude", directory("CLAUDE_CONFIG_DIR", home / ".claude")),
        client("Codex CLI", directory("CODEX_HOME", home / ".codex") / "config.toml", "codex"),
        client("Pi", directory("PI_CODING_AGENT_DIR", home / ".pi/agent") / "mcp.json", "pi"),
        client("Claude Desktop", desktop),
        client("Cursor", home / ".cursor/mcp.json", "cursor"),
        client("OpenCode", opencode, "opencode"),
    ]


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _save(path: Path, original: bytes | None, text: str):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".kitsune-", dir=path.parent) as tmp:
        staged = Path(tmp) / path.name
        if os.name == "nt":
            from .private_files import secure_file

            staged.touch()
            secure_file(str(staged))  # before copying other servers' credentials into it
        staged.write_bytes(text.encode("utf-8"))
        if _read(path) != original:
            raise ValueError("config changed during setup; close the client and retry")
        if os.name == "nt" and original is not None:
            import win32file

            win32file.ReplaceFile(str(path), str(staged))  # preserve the existing config's ACL
        else:
            staged.chmod(0o600 if original is None else path.stat().st_mode & 0o777)
            staged.replace(path)


def _standard_launcher(command, args) -> bool:
    """Only migrate plain PyPI launchers, not a checkout, extras, or an unrelated namesake."""
    if not isinstance(command, str) or Path(command).name.lower() not in {"uvx", "uvx.exe"}:
        return False
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return False
    if len(args) == 1:
        return args[0].partition("@")[0] == PACKAGE
    return (len(args) == 3 and args[0] == "--from" and args[1].partition("==")[0] == PACKAGE
            and args[2] in {"kitsune", PACKAGE})


def register(client: Client, uvx: str) -> str:
    path = client.config.resolve()  # update the target of a dotfiles symlink, not the link
    original = _read(path)
    codex, opencode = client.name == "Codex CLI", client.name == "OpenCode"
    if codex:
        key = "mcp_servers"
        data = tomlkit.parse(original.decode("utf-8") if original is not None else "")
    else:
        key = "mcp" if opencode else "mcpServers"
        try:
            data = json.loads(original) if original is not None else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at line {exc.lineno}; edit commented configs manually") from exc
    if not isinstance(data, dict):
        raise ValueError("config must be an object")
    servers = data.setdefault(key, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{key} must be an object")
    launch = {"type": "local", "command": [uvx, LATEST]} if opencode else {"command": uvx, "args": [LATEST]}
    if "kitsune" in servers:
        entry = servers["kitsune"]
        if not isinstance(entry, dict):
            raise ValueError("existing kitsune entry is not an object; left unchanged")
        command, args = entry.get("command"), entry.get("args")
        if opencode and isinstance(command, list) and command:
            command, args = command[0], command[1:]
        local = "local" if opencode else "stdio"
        if entry.get("type", local) != local or "url" in entry or not _standard_launcher(command, args):
            raise ValueError("existing kitsune entry uses a custom launcher; update it manually")
        if all(entry.get(k) == v for k, v in launch.items()):
            return "Already configured"
        entry.update(launch)  # keep env, disabled state, approvals and other client options
    else:
        servers["kitsune"] = launch
    text = tomlkit.dumps(data) if codex else json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _save(path, original, text)
    return "Configured"


def run(*, list_only: bool, auto: bool) -> int:
    supported = clients()
    if list_only:
        for client in supported:
            print(client.label, client.config or "", sep="  ")
        return 0
    if auto:
        selected = [c for c in supported if c.detected]
    elif sys.stdin.isatty() and sys.stdout.isatty():
        choices = [questionary.Choice(c.label, value=c, checked=c.detected,
                                      disabled=None if c.config else "unavailable") for c in supported]
        try:
            selected = questionary.checkbox(
                "Which clients should use Kitsune?", choices,
                instruction="(Up/Down to move, Space to toggle, Enter to apply, Ctrl+C to cancel)",
            ).unsafe_ask()
        except (KeyboardInterrupt, EOFError):
            print("Cancelled. No configs changed.")
            return 130
    else:
        print("Interactive setup needs a terminal. Use --list to inspect or --auto to register detected clients.",
              file=sys.stderr)
        return 2

    if not selected:
        print("No supported clients detected." if auto else "No clients selected.")
        return 0
    uvx = shutil.which("uvx")
    if not uvx:
        print("uvx not found on PATH. Install uv, then rerun setup.", file=sys.stderr)
        return 1
    uvx = os.path.abspath(uvx)  # GUI clients may not inherit the shell's PATH
    failures = 0
    for client in selected:
        try:
            result = register(client, uvx)
            print(f"{client.name}: {result} ({client.config})")
        except (OSError, ValueError, tomlkit.exceptions.TOMLKitError) as exc:
            print(f"{client.name}: {client.config}: {exc}", file=sys.stderr)
            failures += 1
    if failures < len(selected):
        print("Restart the configured clients (Pi: /reload).\n"
              "Browser extension: https://github.com/junyeong-nero/kitsune/releases/latest")
    return 1 if failures else 0
