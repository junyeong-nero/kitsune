"""Record one take: take.py SCENE.json NAME. Needs `stage.py runner`, `up` and `terminal` first.

Starts an ffmpeg capture of the stage through the runner, types the prompt, waits until the agent
has opened the scene's tabs and gone quiet, then opens each `review` tab as the user would, stops
the capture and saves the bridge audit log for sync.py.
"""
import json
import subprocess
import sys
import time

from common import LOGS, QUEUE, TAKES, WORK, api, load, run_in_runner

MAX_SECONDS = 600


def log(*args):
    print(time.strftime("%T"), *args, flush=True)


def main(scene, name):
    TAKES.mkdir(parents=True, exist_ok=True)
    out = TAKES / f"{name}.mkv"
    display = scene["display"]
    x, y, w, h = scene["stage"]
    ox, oy = display["origin"]
    k = display["scale"]
    crop = f"{w * k}:{h * k}:{(x - ox) * k}:{(y - oy) * k}"
    # The sync line maps the capture clock (CLOCK_UPTIME_RAW, as AVFoundation stamps frames) to wall time.
    script = f"""/usr/bin/python3 -c 'import time; print("sync wall", time.time(), "uptime", time.clock_gettime(time.CLOCK_UPTIME_RAW))'
exec ffmpeg -hide_banner -f avfoundation -capture_cursor 0 -framerate 30 -i "{display['avfoundation_device']}:none" \\
  -vf "crop={crop}" -c:v h264_videotoolbox -b:v 30M -pix_fmt yuv420p -fps_mode passthrough -y {out}
"""
    out.unlink(missing_ok=True)
    run_in_runner(name, script, timeout=0)
    t_start = time.time()
    while time.time() - t_start < 15 and not (out.exists() and out.stat().st_size > 0):
        time.sleep(0.2)
    if not out.exists():
        sys.exit("the capture did not start: " + (QUEUE / f"{name}.out").read_text())
    log("recording", out)
    time.sleep(2.5)
    (WORK / "go").touch()
    log("typing the prompt")

    entered = None
    done = scene["done_when"]
    while time.time() - t_start < MAX_SECONDS:
        events = [json.loads(line) for line in (LOGS / "typer.jsonl").read_text().splitlines() if line.strip()]
        entered = entered or next((e["t"] for e in events if e["ev"] == "enter"), None)
        if entered:
            last_out = max(e["t"] for e in events if e["ev"] == "out")
            audit = api("POST", "/api/audit", {"limit": 300})[1]
            entries = audit.get("entries", audit) if isinstance(audit, dict) else audit
            opened = [e for e in entries if e.get("action") == "newTab" and e["ts"] / 1000 > t_start]
            if len(opened) >= done["min_new_tabs"] and time.time() - last_out > done["idle_seconds"]:
                log("agent finished after opening", len(opened), "tabs")
                break
        time.sleep(1)
    else:
        log("timed out waiting for the agent")

    time.sleep(2)
    tabs = api("GET", "/api/tabs")[1]["tabs"]
    for needle, hold in scene.get("review", []):
        tab = next((t for t in tabs if needle in (t.get("url") or "")), None)
        if tab:
            api("POST", "/api/switch-tab", {"tabId": tab["id"]})
            log("user opens", needle)
            time.sleep(hold)
        else:
            log("no tab for", needle)

    subprocess.run(["pkill", "-INT", "-f", f"ffmpeg.*{out}"])  # ffmpeg finishes the file on SIGINT
    deadline = time.time() + 30
    while time.time() < deadline and not (QUEUE / f"{name}.done").exists():
        time.sleep(0.3)
    log("stopped", out, out.stat().st_size, "bytes")
    audit = api("POST", "/api/audit", {"limit": 500})[1]
    (LOGS / f"{name}-bridge.json").write_text(json.dumps({"audit": audit, "tabs": api("GET", "/api/tabs")[1]}))


if __name__ == "__main__":
    main(load(sys.argv[1]), sys.argv[2])
