"""Per-session status files, read by the status line.

The status line renders often and must stay fast, so it never touches the
database: hooks write a small JSON file per session and it reads that.
"""

import json
import os
import re
import time
from datetime import datetime

from . import ledger, paths

MAX_AGE = 7 * 86400
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def path_for(session_id):
    return paths.status_dir() / f"{_SAFE.sub('_', session_id)}.json"


def write(conn, cfg, session_id, now=None):
    """Recompute today's figures for the session and store them. Returns them.

    `day` lets a reader tell yesterday's figures from today's after midnight.
    """
    now = now or time.time()
    info = dict(ledger.today(conn, cfg, session_id, now), day=datetime.fromtimestamp(now).date().isoformat())
    target = path_for(session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(dict(info, v=1)), encoding="utf-8")
    tmp.replace(target)
    return info


def age(session_id, now):
    """Seconds since the session's figures were computed; infinite if never."""
    try:
        return now - json.loads(path_for(session_id).read_text(encoding="utf-8"))["updated"]
    except (OSError, ValueError, KeyError, TypeError):
        return float("inf")


def cleanup(now=None):
    """Drop files of sessions that ended long ago."""
    now = now or time.time()
    try:
        for p in paths.status_dir().glob("*.json"):
            if now - p.stat().st_mtime > MAX_AGE:
                p.unlink(missing_ok=True)
    except OSError:
        pass
