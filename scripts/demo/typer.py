"""Run a TUI in a pty, log its output with timestamps, and type a prompt when GO_FILE appears.

usage: typer.py LOG.jsonl PROMPT.txt GO_FILE -- command args...
"""
import fcntl
import json
import os
import pty
import random
import re
import select
import signal
import sys
import termios
import time
import tty

log_path, prompt_path, go_file = sys.argv[1:4]
argv = sys.argv[sys.argv.index("--") + 1:]
prompt = open(prompt_path).read().strip()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[@-_]")
log = open(log_path, "a", buffering=1)


def event(**fields):
    log.write(json.dumps({"t": time.time(), **fields}) + "\n")


def winsize(fd):
    return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)


pid, master = pty.fork()
if pid == 0:
    os.execvp(argv[0], argv)

stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
fcntl.ioctl(master, termios.TIOCSWINSZ, winsize(stdin))
signal.signal(signal.SIGWINCH, lambda *_: fcntl.ioctl(master, termios.TIOCSWINSZ, winsize(stdin)))
saved = termios.tcgetattr(stdin)
tty.setraw(stdin)
event(ev="start", argv=argv)

queue = []          # (due_time, bytes) keystrokes still to send
typing_scheduled = False
sent = 0
try:
    while True:
        timeout = 0.02 if queue or not typing_scheduled else 0.2
        ready, _, _ = select.select([master, stdin], [], [], timeout)
        if master in ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            os.write(stdout, data)
            text = ANSI.sub("", data.decode("utf-8", "replace"))
            event(ev="out", n=len(data), text=" ".join(text.split())[:400])
        if stdin in ready:
            data = os.read(stdin, 1024)
            if data:
                os.write(master, data)
        now = time.time()
        if not typing_scheduled and os.path.exists(go_file):
            typing_scheduled = True
            t = now + 0.6
            event(ev="go")
            for ch in prompt:
                queue.append((t, ch.encode()))
                t += random.uniform(0.018, 0.04) + (0.035 if ch in " ,." else 0)
            queue.append((t + 0.7, b"\r"))
        while queue and queue[0][0] <= now:
            _, key = queue.pop(0)
            os.write(master, key)
            sent += 1
            if sent == 1:
                event(ev="type_start")
            if key == b"\r":
                event(ev="enter")
            elif sent == len(prompt):
                event(ev="type_end")
finally:
    termios.tcsetattr(stdin, termios.TCSAFLUSH, saved)
    event(ev="exit")
