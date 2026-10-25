"""Stage the README demo: an isolated bridge, a visible Zen on a fresh profile, and Claude Code.

usage: stage.py SCENE.json runner | up | terminal | reset | down | status

  runner    open the Ghostty job runner (screen capture runs through it)
  up        start the demo bridge and Zen, place Zen on the stage, run the scene's setup clicks
  terminal  open Claude Code in a new Ghostty window and fit Zen beside it
  reset     close the agent's tabs and show the first tab again, for another take
  down      stop the bridge, Zen, the demo terminal and the runner

Never touches the user's own bridge (ports 47651/47652) or browser profiles.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

from common import HERE, HTTP_PORT, LOGS, PYTHON, QUEUE, REPO, STATE, WORK, WS_PORT, api, load, windows
from real_browser_harness import BASE_PREFS, DEFAULT_WS_URL, RemoteDebugger, check_extension_copy
from test_bridge_security import _free_port

ZEN = "/Applications/Zen.app/Contents/MacOS/zen"
GHOSTTY = "/Applications/Ghostty.app"
PREFS = {
    **BASE_PREFS,
    "zen.welcome-screen.seen": True,
    "zen.view.sidebar-expanded": True,
    "zen.view.compact.should-enable-at-startup": False,
    "zen.view.window.scheme": 0,
    "browser.tabs.warnOnClose": False,
    "browser.warnOnQuit": False,
    "browser.translations.automaticallyPopup": False,
    "signon.rememberSignons": False,
    "browser.toolbars.bookmarks.visibility": "never",
}
OVERLAP = 8   # points the browser overlaps the terminal, hiding both windows' inner rounded corners


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(**fields):
    STATE.write_text(json.dumps({**state(), **fields}))


def spawn(name, argv, env=None, cwd=None):
    log = open(LOGS / f"{name}.log", "ab")
    return subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)


def ghostty(*args):
    subprocess.run(["open", "-na", GHOSTTY, "--args", "--config-default-files=false", "--confirm-close-surface=false",
                    "--window-save-state=never", "--quit-after-last-window-closed=true", *args], check=True)


def runner(scene):
    # Below the stage when there is room, otherwise behind the demo windows, which open later.
    x, y, _, height = scene["stage"]
    ghostty(f"--command=/usr/bin/env KITSUNE_DEMO_DIR={WORK} /usr/bin/python3 {HERE / 'runner.py'}",
            f"--window-position-x={x}", f"--window-position-y={y + height - 200}", "--window-width=70", "--window-height=6",
            "--title=kitsune demo runner")
    print("runner started; screen capture is attributed to this new Ghostty instance")


def place_browser(x, y, width, height):
    # Right after another app activates, the window manager can nudge the first move; retry once.
    debugger = RemoteDebugger(state()["rdp_port"])
    try:
        for _ in range(3):
            bounds = debugger.evaluate_in_parent(f"""(async () => {{
                const w = Services.wm.getMostRecentWindow("navigator:browser");
                w.resizeTo({width}, {height}); w.moveTo({x}, {y});
                await new Promise(r => setTimeout(r, 500));
                return [w.screenX, w.screenY, w.outerWidth, w.outerHeight];
            }})()""")
            if bounds == [x, y, width, height]:
                break
    finally:
        debugger.close()
    save(browser_window=bounds)
    return bounds


def up(scene):
    for port in (HTTP_PORT, WS_PORT):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                sys.exit(f"port {port} is busy; run `stage.py SCENE down` first")
    for sub in ("bridge-home", "browser"):
        shutil.rmtree(WORK / sub, ignore_errors=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    bridge_home, browser_root = WORK / "bridge-home", WORK / "browser"
    extension, profile, home = browser_root / "extension", browser_root / "profile", browser_root / "home"
    bridge_home.mkdir(mode=0o700)

    # A developer-build copy that reaches only the demo bridge.
    shutil.copytree(REPO / "extension", extension)
    background = extension / "background" / "background.js"
    source = background.read_text()
    assert source.count(DEFAULT_WS_URL) == 1, "background.js no longer declares WS_URL once"
    background.write_text(source.replace(DEFAULT_WS_URL, f"ws://127.0.0.1:{WS_PORT}"))
    check_extension_copy(extension, WS_PORT)

    env = {k: v for k, v in os.environ.items() if not k.startswith("KITSUNE_")}
    env.update(HOME=str(bridge_home), KITSUNE_HOME=str(bridge_home), KITSUNE_HTTP_PORT=str(HTTP_PORT),
               KITSUNE_WS_PORT=str(WS_PORT), KITSUNE_EXTENSION_DIR=str(extension))
    save(bridge_pid=spawn("bridge", [PYTHON, "-m", "kitsune.bridge"], env=env, cwd=bridge_home).pid)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if (bridge_home / "token").exists() and api("GET", "/api/status")[0] == 200:
                break
        except OSError:
            pass
        time.sleep(0.2)
    else:
        sys.exit(f"demo bridge did not start; see {LOGS / 'bridge.log'}")

    profile.mkdir(parents=True)
    home.mkdir()
    prefs = {**PREFS, **scene.get("browser_prefs", {})}
    (profile / "user.js").write_text("".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n" for k, v in prefs.items()))
    rdp = _free_port()
    benv = {k: v for k, v in os.environ.items() if not k.startswith(("KITSUNE_", "MOZ_"))}
    benv.update(HOME=str(home), MOZ_NO_REMOTE="1")
    browser = spawn("browser", [ZEN, "-no-remote", "-new-instance", "-profile", str(profile),
                                "-start-debugger-server", str(rdp), scene["start_url"]], env=benv, cwd=browser_root)
    save(browser_pid=browser.pid, rdp_port=rdp)
    debugger = RemoteDebugger(rdp)
    try:
        debugger.install_temporary_addon(extension)
    finally:
        debugger.close()
    print("browser at", place_browser(*scene["stage"]))

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = api("GET", "/api/status")[1]
        if status.get("extension_connected") and status.get("extension_generation", 0) > 0 \
                and api("GET", "/api/tabs")[0] == 200:
            break
        time.sleep(0.3)
    else:
        sys.exit(f"the extension did not connect to the demo bridge; see {LOGS / 'browser.log'}")
    time.sleep(2)
    first = api("GET", "/api/tabs")[1]["tabs"][0]["id"]
    for selector in scene.get("setup_clicks", []):
        print("setup click", selector, api("POST", "/api/click", {"tabId": first, "selector": selector})[1].get("ok"))
    print(json.dumps(api("GET", "/api/tabs")[1]["tabs"]))


def terminal(scene):
    LOGS.mkdir(parents=True, exist_ok=True)
    (WORK / "prompt.txt").write_text(scene["prompt"])
    mcp_env = {"KITSUNE_HOME": str(WORK / "bridge-home"), "KITSUNE_HTTP_PORT": str(HTTP_PORT),
               "KITSUNE_WS_PORT": str(WS_PORT), "KITSUNE_EXTENSION_DIR": str(WORK / "browser" / "extension"),
               "KITSUNE_NO_BRIDGE_RESTART": "1", **scene.get("mcp_env", {})}
    (WORK / "mcp.json").write_text(json.dumps({"mcpServers": {"kitsune": {
        "type": "stdio", "command": shutil.which("uv"), "args": ["run", "--directory", str(REPO), "kitsune"],
        "env": mcp_env}}}, indent=2))
    # Claude Code with only the demo Kitsune server; user settings (hooks, status line) stay out of view.
    claude = ["claude", "--mcp-config", str(WORK / "mcp.json"), "--strict-mcp-config", "--allowedTools", "mcp__kitsune",
              "--tools", '""', "--setting-sources", "local", "--model", scene.get("model", "opus")]
    agent = WORK / "agent.sh"
    agent.write_text(f"""#!/bin/bash
cd {REPO} || exit 1
rm -f {WORK / 'go'}
printf '\\033[2J\\033[3J\\033[H'
exec {PYTHON} {HERE / 'typer.py'} {LOGS / 'typer.jsonl'} {WORK / 'prompt.txt'} {WORK / 'go'} -- {' '.join(claude)}
""")
    agent.chmod(0o755)
    (LOGS / "typer.jsonl").unlink(missing_ok=True)
    x, y, width, height = scene["stage"]
    t = scene["terminal"]
    ghostty(f"--theme={t['theme']}", f"--font-size={t['font_size']}", "--window-padding-x=14", "--window-padding-y=10",
            f"--window-width={t['columns']}", "--window-height=80", f"--window-position-x={x}",
            f"--window-position-y={y}", "--title=Claude Code", f"--command={agent}")
    pid = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and pid is None:
        time.sleep(1)
        found = subprocess.run(["pgrep", "-f", f"ghostty.*{agent}"], capture_output=True, text=True).stdout.split()
        pid = int(found[0]) if found else None
    win = None
    while time.monotonic() < deadline and win is None:
        win = next((w for w in windows() if w["pid"] == pid), None)
        time.sleep(1)
    if win is None:
        sys.exit("the demo terminal did not open on screen; see docs/recording-the-demo.md")
    save(terminal_pid=pid, terminal_window=win)
    # Ghostty may clamp the window to the display's right edge; put the browser on whichever side is free.
    if win["x"] > x + width / 2:
        bounds = place_browser(x, y, int(win["x"]) + OVERLAP - x, height)
    else:
        right = int(win["x"] + win["w"]) - OVERLAP
        bounds = place_browser(right, y, x + width - right, height)
    print("terminal", win, "browser", bounds)


def reset(scene):
    tabs = api("GET", "/api/tabs")[1]["tabs"]
    for tab in tabs[1:]:
        api("POST", "/api/close-tab", {"tabId": tab["id"]})
    api("POST", "/api/switch-tab", {"tabId": tabs[0]["id"]})
    subprocess.run(["pkill", "-TERM", "-f", f"ghostty.*{WORK / 'agent.sh'}"])
    print("closed", len(tabs) - 1, "agent tabs; run `terminal` again for a fresh Claude Code session")


def down(scene):
    try:
        api("POST", "/api/shutdown")
    except OSError:
        pass
    for key in ("browser_pid", "bridge_pid"):
        if state().get(key):
            try:
                os.killpg(state()[key], signal.SIGTERM)
            except ProcessLookupError:
                pass
    subprocess.run(["pkill", "-TERM", "-f", f"ghostty.*{WORK / 'agent.sh'}"])
    QUEUE.mkdir(parents=True, exist_ok=True)
    (QUEUE / "QUIT").touch()
    print("stopped the demo bridge, browser, terminal and runner")


if __name__ == "__main__":
    scene_path, command = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "status"
    scene = load(scene_path)
    WORK.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    if command == "status":
        print(json.dumps(api("GET", "/api/status")[1], indent=1))
    else:
        {"runner": runner, "up": up, "terminal": terminal, "reset": reset, "down": down}[command](scene)
