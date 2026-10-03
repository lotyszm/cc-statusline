"""Counted time of tasks since the first event, without counting the past again.

Counting from the first event reads every event ever recorded, and the work list
asks for it on every filter change and keystroke. Days before today are kept in
task_time once counted; a request counts only from the earliest day that may
have changed since, normally today, with the same ledger.allocate as reports.
Counting from a midnight gives the same time after it as counting from the first
event: intervals() loads tool_cap + idle before its start, and nothing earlier
can credit time past that midnight.

A kept day changes when an event lands in it or reaches back into it (an import,
a session resumed days later, a tool that ends after midnight), when an
assignment starts in it, or when a session learns its project. Those are found
from what was added since the last count. What labels time everywhere - the
config, the time zone, this code, ticket aliases, the work list's clients, an
assignment rewritten by a move - makes the basis, and a new basis counts again
from the first event.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import config, db, ledger, tasks, timeline

_code = None


def seconds(conn, cfg, now=None):
    """{(task, session_id): counted seconds} since the first event, as reports count them."""
    now = now or time.time()
    start = _stale_from(conn, cfg, now, _state(conn))
    if start < ledger.local_midnight(now):
        counted = _keep(conn, cfg, now)
        if counted is not None:
            return counted
    return _add(_kept(conn, start), _per_task(ledger.allocate(conn, cfg, start, now)))


def _state(conn):
    return conn.execute("SELECT * FROM task_time_state WHERE id = 1").fetchone()


def _keep(conn, cfg, now):
    """Count from the earliest stale day under the write lock and keep the days before today.

    None when the database cannot be written: read-only, or locked for too long.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return None
    try:
        state = _state(conn)            # another process may have counted meanwhile
        if state is not None and now < state["computed_at"]:
            # A clock that went back, or a test: count from the first event, keep nothing.
            conn.execute("ROLLBACK")
            return None
        start = _stale_from(conn, cfg, now, state)
        cursor = conn.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0]
        assign_max = conn.execute("SELECT coalesce(max(id), 0) FROM assignments").fetchone()[0]
        null_dirs = [r[0] for r in conn.execute("SELECT session_id FROM sessions WHERE project_dir IS NULL")]
        fresh = ledger.allocate(conn, cfg, start, now)

        today = _day(now)
        rows = {}
        for (key, day), secs in fresh.items():
            if key.task and day < today:
                k = (day, key.task, key.session_id)
                rows[k] = rows.get(k, 0.0) + secs
        conn.execute("DELETE FROM task_time WHERE day >= ?", (_day(start) if start > 0 else "",))
        conn.executemany("INSERT INTO task_time (day, task, session_id, seconds) VALUES (?, ?, ?, ?)",
                         [(*k, v) for k, v in rows.items()])
        conn.execute("DELETE FROM task_time_state")
        conn.execute("INSERT INTO task_time_state (id, basis, through, cursor, loaded_to, assign_max, null_dirs, "
                     "computed_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                     (_basis(conn, cfg, assign_max), ledger.local_midnight(now), cursor,
                      now + cfg.tool_cap + cfg.idle, assign_max, json.dumps(null_dirs), now))
        counted = _add(_kept(conn, start), _per_task(fresh))
        conn.execute("COMMIT")
        return counted
    except sqlite3.OperationalError:
        conn.execute("ROLLBACK")
        return None
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _stale_from(conn, cfg, now, state):
    """The local midnight from which kept days may be out of date; 0 for all of them."""
    if state is None or now < state["computed_at"] or _basis(conn, cfg, state["assign_max"]) != state["basis"]:
        return 0.0
    through = state["through"]
    first = min(through, _events_reach(conn, state["cursor"], state["loaded_to"], through))
    row = conn.execute("SELECT min(effective_from) FROM assignments WHERE id > ?", (state["assign_max"],)).fetchone()
    if row[0] is not None:
        first = min(first, row[0])
    nulls = json.loads(state["null_dirs"])
    for i in range(0, len(nulls), 500):
        chunk = nulls[i:i + 500]
        row = conn.execute(f"SELECT min(e.ts) FROM sessions s JOIN events e ON e.session_id = s.session_id "
                           f"WHERE s.project_dir IS NOT NULL AND s.session_id IN ({', '.join('?' * len(chunk))})",
                           chunk).fetchone()
        if row[0] is not None:
            first = min(first, row[0])
    return ledger.local_midnight(first)


def _events_reach(conn, cursor, loaded_to, bound):
    """How far back events added since the last count reach; `bound` if not before it.

    New are events past the cursor, and those the last count did not load
    because they lay beyond its margin. Each reaches back to the session's event
    before it, which now has a next event to count to, and a tool end to the start
    of its tool, which from then on was running.
    """
    # Rows, grouped here: GROUP BY session_id in SQL walks the whole session index.
    earliest, ends = {}, {}
    for sid, ts, kind, tool_use_id in conn.execute(
            "SELECT session_id, ts, kind, tool_use_id FROM events WHERE id > ? "
            "UNION ALL SELECT session_id, ts, kind, tool_use_id FROM events WHERE ts >= ?", (cursor, loaded_to)):
        earliest[sid] = min(ts, earliest.get(sid, ts))
        if kind == "tool_end" and tool_use_id:
            ends.setdefault(sid, set()).add(tool_use_id)
    first = bound
    for sid, ts in earliest.items():
        prev = conn.execute("SELECT max(ts) FROM events WHERE session_id = ? AND ts < ?", (sid, ts)).fetchone()[0]
        first = min(first, ts, ts if prev is None else prev)
        if sid in ends:
            for tool_use_id, start in conn.execute("SELECT tool_use_id, ts FROM events WHERE session_id = ? "
                                                   "AND ts < ? AND kind = 'tool_start'", (sid, bound)):
                if tool_use_id in ends[sid]:
                    first = min(first, start)
    return first


def _basis(conn, cfg, assign_max):
    settings = {k: v for k, v in vars(cfg).items() if k not in ("_seen", "error", "currency", "rates")}
    assigned = [tuple(r) for r in conn.execute(
        "SELECT id, session_id, task, effective_from, branch FROM assignments WHERE id <= ? ORDER BY id",
        (assign_max,))]
    zone = [os.environ.get("TZ"), time.tzname, time.timezone, time.altzone]
    data = json.dumps([_code_hash(), zone, settings, sorted(db.task_aliases(conn).items()),
                       sorted(db.project_clients(conn)), assigned], default=_plain, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _plain(value):
    """JSON for what the config holds: patterns, sets and rules."""
    if isinstance(value, re.Pattern):
        return [value.pattern, value.flags]
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return vars(value)


def _code_hash():
    """The source of the modules that count time, read once per process."""
    global _code
    if _code is None:
        h = hashlib.sha256()
        for path in (ledger.__file__, timeline.__file__, tasks.__file__, config.__file__, db.__file__, __file__):
            h.update(Path(path).read_bytes())
        _code = h.hexdigest()
    return _code


def _day(ts):
    return datetime.fromtimestamp(ts).date().isoformat()


def _kept(conn, start):
    """{(task, session_id): seconds} kept for the days before `start`."""
    if start <= 0:
        return {}
    return {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT task, session_id, sum(seconds) FROM task_time WHERE day < ? GROUP BY task, session_id",
        (_day(start),))}


def _per_task(alloc):
    out = {}
    for (key, _day), secs in alloc.items():
        if key.task:
            out[(key.task, key.session_id)] = out.get((key.task, key.session_id), 0.0) + secs
    return out


def _add(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out
