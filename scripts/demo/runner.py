"""Job runner for a Ghostty instance launched after Screen Recording was granted.

Runs every $KITSUNE_DEMO_DIR/q/NAME.sh it finds (bash), writing NAME.out, NAME.pid and NAME.done.
Commands run here are attributed to this fresh Ghostty instance, so screencapture works.
"""
import glob
import os
import subprocess
import sys
import time

Q = os.path.join(os.environ.get("KITSUNE_DEMO_DIR", "/tmp/kitsune-demo"), "q")
os.makedirs(Q, exist_ok=True)
running = {}
print("kitsune demo runner: watching", Q, "(touch QUIT there to stop)", flush=True)
while True:
    for job in sorted(glob.glob(f"{Q}/*.sh")):
        name = job[:-3]
        os.rename(job, name + ".running")
        out = open(name + ".out", "w")
        proc = subprocess.Popen(["/bin/bash", name + ".running"], stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        open(name + ".pid", "w").write(str(proc.pid))
        running[name] = proc
        print(time.strftime("%T"), "started", os.path.basename(name), proc.pid, flush=True)
    for name, proc in list(running.items()):
        if proc.poll() is not None:
            open(name + ".done", "w").write(str(proc.returncode))
            print(time.strftime("%T"), "finished", os.path.basename(name), proc.returncode, flush=True)
            del running[name]
    if os.path.exists(f"{Q}/QUIT"):
        sys.exit(0)
    time.sleep(0.1)
