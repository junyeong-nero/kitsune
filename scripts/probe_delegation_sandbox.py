"""Bounded macOS isolation feasibility probes, NOT a runner or a selected security boundary.

Only authored cases, dummy files and a local canary listener. No Kitsune/browser/model imports.
Results go to stdout; unsuccessful probe directories are retained for inspection.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

PYTHON = str(Path(sys.executable).resolve())
PREFIX = str(Path(sys.base_prefix).resolve())
# python.org's bin/python is a launcher which execs this second executable.
# Invoke the actual interpreter instead of granting a blanket exec exception.
FRAMEWORK_PYTHON = Path(PREFIX) / "Resources/Python.app/Contents/MacOS/Python"
if sys.platform == "darwin" and FRAMEWORK_PYTHON.is_file():
    PYTHON = str(FRAMEWORK_PYTHON.resolve())

# Imports are deliberately not hidden from the worker. Native open/exec/network
# attempts must fail at the OS boundary, not because a friendly SDK omits them.
HEADER = """import ctypes, json, os, pathlib, socket, subprocess, sys
try:
"""
FOOTER = """
except OSError as exc:
    print(json.dumps({'outcome': 'denied' if isinstance(exc, PermissionError) else 'os_error',
                      'errno': exc.errno, 'type': type(exc).__name__,
                      'phase': globals().get('phase')}), flush=True)
"""


def source(body: str) -> str:
    return HEADER + "\n".join("    " + line for line in body.splitlines()) + FOOTER


def run(body, args, env, profile=None, pass_fds=(), timeout=4):
    argv = [PYTHON, "-I", "-S", "-B", "-c", source(body), *map(str, args)]
    if profile:
        argv = ["/usr/bin/sandbox-exec", "-p", profile, *argv]
    process = subprocess.Popen(argv, cwd=env["HOME"], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               close_fds=True, pass_fds=pass_fds, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=2)
        timed_out = True
    try:
        result = json.loads(stdout)
    except ValueError:
        result = {"outcome": "no_json_report", "stdout": stdout[:1000]}
    return {"pid": process.pid, "returncode": process.returncode, "timed_out": timed_out,
            "result": result, "stderr": stderr[:2000]}


def main():
    if sys.platform != "darwin":
        raise SystemExit("This probe is macOS-only; no unrestricted fallback.")
    root = Path(tempfile.mkdtemp(prefix="kitsune-500-seatbelt-")).resolve()
    for directory in ("home", "allowed", "private"):
        (root / directory).mkdir()
    secret = root / "private/token"
    secret.write_text("probe-only-not-a-real-credential")
    (root / "allowed/alias").symlink_to(secret)
    env = {"HOME": str(root / "home"), "LANG": "C", "PATH": "/usr/bin:/bin"}
    quote = json.dumps
    site = str(Path(PREFIX) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    profile = f'''(version 1)
(deny default)
(import "dyld-support.sb")
(allow syscall*)
(allow mach-bootstrap)
(allow sysctl-read
    (sysctl-name-regex #"^hw[.]")
    (sysctl-name "kern.osrelease" "kern.osversion" "kern.ostype" "kern.version" "kern.hostname"))
(allow process-exec (literal {quote(PYTHON)}))
(allow signal (target self))
(allow file-map-executable
    (require-all (subpath {quote(PREFIX)}) (require-not (subpath {quote(site)})))
    (subpath "/usr/lib") (subpath "/System/Library"))
(allow file-read*
    (require-all (subpath {quote(PREFIX)}) (require-not (subpath {quote(site)})))
    (subpath "/usr/lib") (subpath "/System/Library")
    (literal "/dev/null") (subpath {quote(str(root / 'home'))}) (subpath {quote(str(root / 'allowed'))}))
'''
    (root / "profile.sb").write_text(profile)
    read_canary = "pathlib.Path(sys.argv[1]).read_text()\nprint(json.dumps({'outcome': 'allowed'}))"
    reexec_source = 'phase = "after_reexec"\n' + source(read_canary)
    cases = {
        "ordinary_python": "import math, re\nprint(json.dumps({'outcome': 'allowed', 'value': math.isqrt(81)}))",
        "file_read": read_canary,
        "symlink_read": "pathlib.Path(sys.argv[2]).read_text()\nprint(json.dumps({'outcome': 'allowed'}))",
        "file_write": "pathlib.Path(sys.argv[3]).write_text('canary')\nprint(json.dumps({'outcome': 'allowed'}))",
        "native_open": "lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)\nfd = lib.open(os.fsencode(sys.argv[1]), 0)\nif fd < 0: raise OSError(ctypes.get_errno(), 'native open')\nos.close(fd)\nprint(json.dumps({'outcome': 'allowed'}))",
        "tcp": "s = socket.socket()\ns.settimeout(1)\ns.connect(('127.0.0.1', int(sys.argv[4])))\ns.close()\nprint(json.dumps({'outcome': 'allowed'}))",
        "fork": "pid = os.fork()\nif pid == 0: os._exit(0)\nos.waitpid(pid, 0)\nprint(json.dumps({'outcome': 'allowed'}))",
        "subprocess_same_interpreter": "subprocess.run([sys.executable, '-I', '-S', '-c', 'pass'], check=True)\nprint(json.dumps({'outcome': 'allowed'}))",
        "exec_other": "os.execv('/usr/bin/true', ['/usr/bin/true'])",
        "reexec_read": f"os.execv(sys.argv[5], [sys.argv[5], '-I', '-S', '-c', {reexec_source!r}, sys.argv[1]])",
        "replace_policy_then_read": "lib = ctypes.CDLL('/usr/lib/libsandbox.dylib')\nlib.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]\nerror = ctypes.c_char_p()\nrc = lib.sandbox_init(b'(version 1)(allow default)', 0, ctypes.byref(error))\npathlib.Path(sys.argv[1]).read_text()\nprint(json.dumps({'outcome': 'allowed', 'reapply_returncode': rc}))",
        "environment": "print(json.dumps({'outcome': 'allowed', 'has_token': 'KITSUNE_TOKEN' in os.environ, 'home': os.environ['HOME']}))",
    }
    results = {}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        args = [secret, root / "allowed/alias", root / "private/write", listener.getsockname()[1], PYTHON]
        for name, body in cases.items():
            results[name] = {"control": run(body, args, env), "sandbox": run(body, args, env, profile)}
            if name == "ordinary_python" and results[name]["sandbox"]["returncode"] != 0:
                break  # A failed startup is not a passing negative test.
    startup_ok = results["ordinary_python"]["sandbox"]["returncode"] == 0
    # Demonstrate the documented pre-opened-FD caveat using a dummy credential,
    # then show the launcher closes it by default. Neither case uses a real token.
    with secret.open("rb") as handle:
        fd = handle.fileno()
        body = f"os.read({fd}, 1)\nprint(json.dumps({{'outcome': 'allowed'}}))"
        if startup_ok:
            results["inherited_fd"] = {"explicitly_passed": run(body, [], env, profile, (fd,)),
                                       "closed_by_launcher": run(body, [], env, profile)}
    # Deliberately bounded allocations (64 MiB, never an OOM stress test).
    # A limit accepted by setrlimit may still be unenforced on Darwin.
    memory = {}
    for limit in ("RLIMIT_AS", "RLIMIT_DATA", "RLIMIT_RSS"):
        body = f"import resource\n_, hard = resource.getrlimit(resource.{limit})\nresource.setrlimit(resource.{limit}, (32*1024*1024, hard))\nresource.setrlimit(resource.{limit}, (32*1024*1024, 32*1024*1024))\nvalue = bytearray(64*1024*1024)\nprint(json.dumps({{'outcome': 'allocated_64_MiB', 'maxrss': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}}))"
        memory[limit] = {"control": run(body, [], env),
                         "sandbox": run(body, [], env, profile) if startup_ok else {"not_run": "startup_failed"}}
    # Diagnose a soft limit separately; never treat it as a safe hard-limit fallback.
    soft_body = "import resource\n_, hard = resource.getrlimit(resource.RLIMIT_RSS)\nresource.setrlimit(resource.RLIMIT_RSS, (32*1024*1024, hard))\nvalue = bytearray(64*1024*1024)\nprint(json.dumps({'outcome':'allocated_64_MiB', 'limits':resource.getrlimit(resource.RLIMIT_RSS), 'maxrss':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))"
    memory["RSS_soft_only_diagnostic"] = {"control": run(soft_body, [], env),
        "sandbox": run(soft_body, [], env, profile) if startup_ok else {"not_run": "startup_failed"}}
    cpu_body = "import resource\nresource.setrlimit(resource.RLIMIT_CPU, (1, 1))\nwhile True: pass"
    cpu_ignored = "import signal\nsignal.signal(signal.SIGXCPU, signal.SIG_IGN)\n" + cpu_body
    cpu = {"default_signal": run(cpu_body, [], env, profile) if startup_ok else {"not_run": "startup_failed"},
           "ignored_signal": run(cpu_ignored, [], env, profile) if startup_ok else {"not_run": "startup_failed"}}
    report = {"platform": sys.platform, "os_release": os.uname().release,
              "python": sys.version, "executable": PYTHON,
              "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "profile_sha256": hashlib.sha256(profile.encode()).hexdigest(),
              "dyld_support_sha256": hashlib.sha256(Path('/System/Library/Sandbox/Profiles/dyld-support.sb').read_bytes()).hexdigest(),
              "artifact_dir": str(root), "host_access": results, "memory_limits": memory,
              "cpu_limit": cpu, "status": "feasibility_only_not_a_security_verdict"}
    rendered = json.dumps(report, indent=2)
    (root / "report.json").write_text(rendered + "\n")
    print(rendered)
    # Retain even successful probes: FD/memory behavior needs manual interpretation.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
