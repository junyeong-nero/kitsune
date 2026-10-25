"""Print a take's timeline in video seconds, to write the edit: sync.py NAME [TEXT ...]

Lists the typing and Enter times, every bridge action of the take and, for each TEXT, the first
moment it appeared in Claude Code's output. Writes logs/NAME-events.json.
"""
import json
import re
import sys

from common import LOGS, QUEUE


def main(name, texts):
    out = (QUEUE / f"{name}.out").read_text()
    wall, uptime = map(float, re.search(r"sync wall ([\d.]+) uptime ([\d.]+)", out).groups())
    start = float(re.search(r"start: ([\d.]+)", out).group(1))
    t0 = start + (wall - uptime)                       # wall clock of video t=0
    typer = [json.loads(line) for line in (LOGS / "typer.jsonl").read_text().splitlines() if line.strip()]
    bridge = json.loads((LOGS / f"{name}-bridge.json").read_text())
    audit = bridge["audit"].get("entries", bridge["audit"]) if isinstance(bridge["audit"], dict) else bridge["audit"]
    events = [{"t": e["t"] - t0, "kind": e["ev"]} for e in typer if e["ev"] in ("go", "type_start", "type_end", "enter")]
    for e in audit:
        t = e["ts"] / 1000 - t0
        if t >= 0:
            params = e.get("params") or {}
            events.append({"t": t, "kind": e["action"], "tab": params.get("tabId"),
                           "detail": params.get("url") or params.get("element")})
    for text in texts:
        hit = next((e["t"] - t0 for e in typer if e["ev"] == "out" and text in e["text"]), None)
        events.append({"t": hit if hit is not None else float("nan"), "kind": "text", "detail": text})
    outputs = [e["t"] - t0 for e in typer if e["ev"] == "out" and e["t"] >= t0]
    events.sort(key=lambda e: (e["t"] != e["t"], e["t"]))
    (LOGS / f"{name}-events.json").write_text(json.dumps({"t0": t0, "events": events, "outputs": outputs}))
    print(f"last terminal output at {max(outputs):.1f}s")
    for e in events:
        print(f"{e['t']:7.2f}  {e['kind']:10s} {e.get('tab') or ''} {e.get('detail') or ''}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
