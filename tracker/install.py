"""Hooks, the command link and the setup check for time tracking.

statusline.py --install, --uninstall and --doctor drive this module. It edits
the settings dicts it is handed; reading, backing up and writing settings.json
stays in statusline.py, so every account gets one backup per run.
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import paths
from .config import TEMPLATE

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "statusline.py"
COMMAND = "cc-statusline"           # linked into ~/.local/bin, points at SCRIPT

# (event, async). The context events must stay synchronous: their output is
# how the agent learns about tasks. Stop is synchronous too: `claude -p` exits
# right after the reply and cancels async hooks, and Stop is what refreshes the
# status line (about 0.1 s). The others only record and never block.
HOOKS = (
    ("SessionStart", False),
    ("UserPromptSubmit", False),
    ("PreToolUse", True),
    ("PostToolUse", True),
    ("PostToolUseFailure", True),
    ("Notification", True),
    ("Stop", False),
    ("StopFailure", False),
    ("SubagentStart", True),
    ("SubagentStop", True),
    ("SessionEnd", True),
)
TIMEOUT = 10
PERMISSION = f"Bash({COMMAND} task:*)"  # lets the agent log a confirmed task without a prompt
_MARK = 'statusline.py" hook'
_PYTHON = re.compile(r'^"([^"]+)"')

# A python3 at one of these paths survives upgrades of the interpreter itself.
STABLE_PYTHONS = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3")


def command(python, script=SCRIPT):
    # Quoted: Claude Code runs this through a shell and paths may contain spaces.
    return f'"{python}" "{script}" hook'


def is_ours(handler):
    cmd = handler.get("command") if isinstance(handler, dict) else None
    return isinstance(cmd, str) and cmd.rstrip().endswith(_MARK)


def _handlers(settings):
    """(event, handler) for every hook handler in the settings."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else ():
            handlers = group.get("hooks") if isinstance(group, dict) else None
            for h in handlers if isinstance(handlers, list) else ():
                yield event, h


def wired_events(settings):
    return {event for event, h in _handlers(settings) if is_ours(h)}


def hook_python(settings):
    """The interpreter our hooks run with; None when they are not wired."""
    for _, h in _handlers(settings):
        m = _PYTHON.match(h["command"]) if is_ours(h) else None
        if m:
            return m.group(1)
    return None


def stable_python(executable=None, candidates=None):
    """A path to this interpreter that survives upgrades, when there is one.

    Homebrew reports a versioned path (.../opt/python@3.14/bin/python3.14) that
    the next major upgrade removes; /opt/homebrew/bin/python3 keeps working.
    """
    executable = executable or sys.executable
    if candidates is None:
        candidates = (shutil.which("python3"),) + STABLE_PYTHONS
    real = os.path.realpath(executable)
    for c in candidates:
        if c and os.path.exists(c) and os.path.realpath(c) == real:
            return c
    return executable


def _strip(settings):
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        if not isinstance(hooks[event], list):
            continue
        groups = []
        for group in hooks[event]:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list) or not handlers:
                groups.append(group)
                continue
            kept = [h for h in handlers if not is_ours(h)]
            if kept:
                groups.append(dict(group, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]


def wire(settings, cmd):
    """Add our handler to every event (once) and the permission rule."""
    _strip(settings)
    hooks = settings.setdefault("hooks", {})
    for event, is_async in HOOKS:
        handler = {"type": "command", "command": cmd, "timeout": TIMEOUT}
        if is_async:
            handler["async"] = True
        hooks.setdefault(event, []).append({"hooks": [handler]})
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    if PERMISSION not in allow:
        allow.append(PERMISSION)
    return settings


def unwire(settings):
    """Remove what wire() added, and any section left empty by that."""
    _strip(settings)
    perms = settings.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("allow"), list):
        perms["allow"] = [p for p in perms["allow"] if p != PERMISSION]
        if not perms["allow"]:
            del perms["allow"]
        if not perms:
            del settings["permissions"]
    return settings


def _link_path(bin_dir=None):
    return (Path(bin_dir) if bin_dir else Path.home() / ".local" / "bin") / COMMAND


def make_link(bin_dir=None, out=None):
    out = out or sys.stdout
    target = _link_path(bin_dir)
    if target.exists() and not target.is_symlink():
        print(f"command:   {target} exists and is not a link; left alone", file=out)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    target.symlink_to(SCRIPT)
    on_path = str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)
    print(f"command:   {target} -> {SCRIPT}" + ("" if on_path else f"  ({target.parent} is not on PATH)"),
          file=out)


def remove_link(bin_dir=None, out=None):
    out = out or sys.stdout
    target = _link_path(bin_dir)
    if target.is_symlink() and Path(os.path.realpath(target)) == SCRIPT.resolve():
        target.unlink()
        print(f"removed:   {target}", file=out)


def setup(python, link=True, bin_dir=None, out=None):
    """Data dir, the interpreter record, a config template and the command link."""
    out = out or sys.stdout
    data = paths.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    # bin/dashboard.sh starts the server with the interpreter the hooks use.
    (data / "python").write_text(python + "\n", encoding="utf-8")
    cfg = paths.config_path()
    if cfg.exists():
        print(f"config:    {cfg}  (kept)", file=out)
    else:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(TEMPLATE, encoding="utf-8")
        print(f"config:    {cfg}  (template; add your client rules)", file=out)
    if link:
        make_link(bin_dir, out)
    print(f"data:      {data}", file=out)


def _read_settings(path):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def _ago(ts, now):
    if not ts:
        return "never"
    d = int(now - ts)
    if d < 120:
        return f"{d}s ago"
    if d < 7200:
        return f"{d // 60} min ago"
    if d < 172800:
        return f"{d // 3600} h ago"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def doctor(conn, cfg, now, accounts, out=None):
    """Print the time-tracking part of --doctor. True when nothing needs attention."""
    out = out or sys.stdout
    ok = True
    cpath = paths.config_path()
    if cfg.error:
        ok = False
        print(f"config        BROKEN, defaults in use: {cfg.error}", file=out)
    else:
        print(f"config        {cpath}" + ("" if cpath.exists() else "  (missing, defaults in use)"), file=out)
    billable = sum(1 for r in cfg.rules if r.billable)
    print(f"rules         {len(cfg.rules)} ({billable} billable) · break after {cfg.idle / 60:g} min · "
          f"tool cap {cfg.tool_cap / 60:g} min · overlap {cfg.overlap}", file=out)
    if not cfg.rules:
        print("              no client rules yet: time adds up per project and the agent never asks "
              "about tasks", file=out)

    n, n_sessions = conn.execute("SELECT count(*), count(DISTINCT session_id) FROM events").fetchone()
    last_hook = conn.execute("SELECT max(ts) FROM events WHERE source = 'hook'").fetchone()[0]
    print(f"database      {paths.db_path()}  · {n} events, {n_sessions} sessions · "
          f"last hook event {_ago(last_hook, now)}", file=out)

    for acc in accounts:
        try:
            s = _read_settings(Path(acc) / "settings.json")
        except Exception as e:
            ok = False
            print(f"hooks         {acc}  unreadable settings.json ({e})", file=out)
            continue
        wired = wired_events(s)
        missing = [e for e, _ in HOOKS if e not in wired]
        python = hook_python(s)
        if not wired:
            ok = False
            state = "not wired — run statusline.py --install"
        elif missing:
            ok = False
            state = f"partly wired, missing {', '.join(missing)} — run statusline.py --install"
        elif python and not os.access(python, os.X_OK):
            ok = False
            state = f"wired, but {python} is gone — run statusline.py --install"
        else:
            state = "wired"
        print(f"hooks         {acc}  {state}", file=out)

    on_path = shutil.which(COMMAND)
    print(f"command       {on_path or COMMAND + ' not on PATH (the agent gets the full path instead)'}",
          file=out)

    log = paths.log_path()
    if log.exists() and log.stat().st_size:
        lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        stamped = [ln for ln in lines if ln[:4].isdigit() and ln[4:5] == "-"]
        when = stamped[-1][:16] if stamped else "?"
        print(f"hook log      {log}\n              last entry {when}: {lines[-1][:100]}", file=out)
    return ok
