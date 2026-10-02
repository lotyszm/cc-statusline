"""Starts the dashboard at login: a launchd agent on macOS, a systemd user unit
on Linux. Anywhere else it says so and changes nothing.

The service runs `statusline.py dashboard --service`, which listens on
127.0.0.1 only and exits quietly when the port is taken, so the service
manager restarts it after a crash but never loops on a busy port.
"""

import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

LABEL = "com.github.lotyszm.cc-statusline.dashboard"
UNIT = "cc-statusline-dashboard.service"
PORT = 8765
# Settings the service would not see otherwise: it starts outside your shell.
ENV_KEYS = ("CC_STATUSLINE_DATA_DIR", "CC_STATUSLINE_CONFIG", "XDG_DATA_HOME", "XDG_CONFIG_HOME")


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _env():
    return {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}


def log_path():
    return paths.data_dir() / "dashboard.log"


def plist_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def unit_path():
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "systemd" / "user" / UNIT


def program(python, script, port=PORT):
    return [python, str(script), "dashboard", "--service", "--port", str(port)]


def launchd_plist(python, script, port=PORT):
    job = {
        "Label": LABEL,
        "ProgramArguments": program(python, script, port),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
    }
    if _env():
        job["EnvironmentVariables"] = _env()
    return plistlib.dumps(job)


def _systemd_quote(text):
    # systemd expands %specifiers and $VARIABLES inside ExecStart.
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{text}"'


def systemd_unit(python, script, port=PORT):
    log = str(log_path()).replace("%", "%%")
    lines = [
        "[Unit]",
        f"Description=cc-statusline dashboard on http://127.0.0.1:{port}/",
        "",
        "[Service]",
        "ExecStart=" + " ".join(_systemd_quote(a) for a in program(python, script, port)),
        "Restart=on-failure",
        "RestartSec=10",
        *(f"Environment={_systemd_quote(f'{k}={v}')}" for k, v in _env().items()),
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _domain():
    return f"gui/{os.getuid()}"


def enable(python, script, port=PORT, run=_run, out=None, system=None):
    """Install the service and start it now. True when it was loaded."""
    out = out or sys.stdout
    system = system or platform.system()
    url = f"http://127.0.0.1:{port}/"
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    if system == "Darwin":
        plist = plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(launchd_plist(python, script, port))
        run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])   # reload if it was loaded
        for attempt in range(10):
            # bootout returns before the old job is gone; bootstrap fails until then.
            r = run(["launchctl", "bootstrap", _domain(), str(plist)])
            if r.returncode == 0:
                print(f"dashboard: {url}  (starts at login: launchd {LABEL})", file=out)
                return True
            time.sleep(0.2)
        print(f"dashboard: launchctl bootstrap failed: {(r.stderr or r.stdout).strip()}", file=out)
        return False
    if system == "Linux":
        if not shutil.which("systemctl"):
            print("dashboard: no systemd here; start it with bin/dashboard.sh when you need it", file=out)
            return False
        unit = unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(python, script, port), encoding="utf-8")
        run(["systemctl", "--user", "daemon-reload"])
        r = run(["systemctl", "--user", "enable", UNIT])
        if r.returncode == 0:
            r = run(["systemctl", "--user", "restart", UNIT])
        if r.returncode == 0:
            print(f"dashboard: {url}  (starts at login: systemd --user {UNIT})", file=out)
            return True
        print(f"dashboard: systemctl failed: {(r.stderr or r.stdout).strip()}", file=out)
        return False
    print(f"dashboard: autostart is not supported on {system}; run `cc-statusline dashboard`", file=out)
    return False


def disable(run=_run, out=None, system=None):
    """Stop the service and remove it. True when there was one."""
    out = out or sys.stdout
    system = system or platform.system()
    if system == "Darwin" and plist_path().exists():
        run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])
        plist_path().unlink()
        print(f"dashboard: autostart removed ({LABEL})", file=out)
        return True
    if system == "Linux" and unit_path().exists():
        run(["systemctl", "--user", "disable", "--now", UNIT])
        unit_path().unlink()
        run(["systemctl", "--user", "daemon-reload"])
        print(f"dashboard: autostart removed ({UNIT})", file=out)
        return True
    return False


def status(run=_run, system=None):
    """{'manager', 'installed', 'running', 'path'} for --doctor."""
    system = system or platform.system()
    if system == "Darwin":
        installed = plist_path().exists()
        running = installed and run(["launchctl", "print", f"{_domain()}/{LABEL}"]).returncode == 0
        return {"manager": "launchd", "installed": installed, "running": running, "path": plist_path()}
    if system == "Linux":
        installed = unit_path().exists()
        running = installed and run(["systemctl", "--user", "is-active", UNIT]).returncode == 0
        return {"manager": "systemd", "installed": installed, "running": running, "path": unit_path()}
    return {"manager": None, "installed": False, "running": False, "path": None}
