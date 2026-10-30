"""Opt-in, read-only browser build comparison for browser_status (#366).

Runtime values come from the connected extension, never a page's user agent. On macOS,
identify ONLY the process owning the reverse of that WebSocket's TCP connection, then
read that process's app bundle. No global browser-name search, profile reads, or restart.
Other platforms and incomplete evidence explicitly return unknown. Paths, PIDs, socket
addresses and subprocess stderr are not returned to the caller.
"""

import configparser
import ipaddress
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from xml.parsers.expat import ExpatError

PROCESS_TIMEOUT = 1
METADATA_LIMIT = 65536
RUNTIME_SOURCE = "browser.runtime.getBrowserInfo"
INSTALLED_SOURCE = "application.ini [App]"
RESTART_HINT = (
    "The connected browser's runtime and installed build differ. Ask the user to save work and "
    "restart the browser, then check browser_status(diagnostics=true) and list tabs again. "
    "This is evidence of a pending browser restart, not proof that it caused a particular page failure."
)


def browser_info(value):
    """Keep bounded browser metadata only; ignore arbitrary extension payload fields."""
    if not isinstance(value, dict):
        return {}
    return {key: text for key in ("name", "vendor", "version", "buildID")
            if isinstance(text := value.get(key), str) and 0 < len(text) <= 128
            and all(char.isprintable() for char in text)}


def unknown(reason, runtime=None):
    result = {"status": "unknown", "reason": reason, "restart_recommended": None}
    if runtime:
        result["runtime"] = {**browser_info(runtime), "source": RUNTIME_SOURCE}
    return result


class Unavailable(Exception):
    """A public reason code, never raw OS exception text."""


def _lsof(*arguments):
    try:
        result = subprocess.run(["/usr/sbin/lsof", *arguments], capture_output=True, text=True,
                                errors="replace", timeout=PROCESS_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise Unavailable("process_lookup_timeout") from None
    except OSError:
        raise Unavailable("process_lookup_unavailable") from None
    if result.returncode not in (0, 1):
        raise Unavailable("process_lookup_failed")
    return result.stdout


def _endpoint(address):
    try:
        host, port = address[:2]
        ip = ipaddress.ip_address(host)
        if not ip.is_loopback or type(port) is not int or not 0 < port < 65536:
            raise ValueError
        return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"
    except (TypeError, ValueError, IndexError):
        raise Unavailable("connection_address_unavailable") from None


def _peer_pid(local, remote):
    # lsof also lists the bridge's forward connection: accept only the exact reverse.
    expected = f"{_endpoint(remote)}->{_endpoint(local)}"
    output = _lsof("-nP", "-a", f"-iTCP:{local[1]}", "-sTCP:ESTABLISHED", "-Fpn")
    pid = None
    matches = set()
    for line in output.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line == "n" + expected and pid:
            matches.add(pid)
    if len(matches) != 1:
        raise Unavailable("browser_process_unavailable" if not matches else "ambiguous_browser_process")
    return matches.pop()


def _read_small(path):
    with path.open("rb") as stream:
        data = stream.read(METADATA_LIMIT + 1)
    if len(data) > METADATA_LIMIT:
        raise ValueError("oversized metadata")
    return data


def _installed_info(pid):
    output = _lsof("-a", "-p", str(pid), "-d", "txt", "-Fn")
    bundles = set()
    for line in output.splitlines():
        match = re.fullmatch(r"n(/.+\.app)/(Contents(?:\.bak)?)/MacOS/([^/]+)", line)
        if not match:
            continue
        bundle, contents, executable = match.groups()
        try:
            metadata = plistlib.loads(_read_small(Path(bundle, contents, "Info.plist")))
            if isinstance(metadata, dict) and metadata.get("CFBundleExecutable") == executable:
                bundles.add((bundle, contents))
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError):
            continue
    if len(bundles) != 1:
        raise Unavailable("browser_bundle_unavailable")
    bundle, contents = bundles.pop()
    try:
        ini = configparser.ConfigParser(interpolation=None)
        ini.read_string(_read_small(Path(bundle, "Contents", "Resources", "application.ini")).decode("utf-8"))
        app = ini["App"]
        info = browser_info({"name": app.get("Name"), "vendor": app.get("Vendor"),
                             "version": app.get("Version"), "buildID": app.get("BuildID")})
    except (OSError, ValueError, KeyError, configparser.Error):
        raise Unavailable("installed_metadata_unavailable") from None
    return {**info, "source": INSTALLED_SOURCE}, "backup" if contents == "Contents.bak" else "current"


def diagnose(runtime, local, remote):
    runtime = browser_info(runtime)
    if not runtime:
        return unknown("runtime_info_unavailable")
    if sys.platform != "darwin":
        return unknown("unsupported_platform", runtime)
    try:
        pid = _peer_pid(local, remote)
        installed, mapped_bundle = _installed_info(pid)
        # A process can exit while its files are inspected. Do not reuse a PID's metadata
        # unless it still owns the same connection; the bridge also checks WS generation.
        if _peer_pid(local, remote) != pid:
            return unknown("connection_changed", runtime)
    except Unavailable as error:
        return unknown(str(error), runtime)
    result = {"runtime": {**runtime, "source": RUNTIME_SOURCE}, "installed": installed,
              "process": {"source": "websocket_peer", "mapped_bundle": mapped_bundle}}
    # These are the same Gecko app-build identifier. Product/engine version strings can
    # use different namespaces in Firefox forks, so never fall back to comparing them.
    builds = [info.get("buildID", "") for info in (runtime, installed)]
    if not all(re.fullmatch(r"[0-9]{14}", build) for build in builds):
        return {**result, **unknown("build_id_unavailable"), "comparison": "gecko_build_id"}
    mismatch = builds[0] != builds[1]
    return {**result, "status": "mismatch" if mismatch else "match", "comparison": "gecko_build_id",
            "restart_recommended": mismatch,
            **({"hint": RESTART_HINT} if mismatch else {})}
