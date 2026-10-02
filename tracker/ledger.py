"""Labels credited time with client, project, branch and task.

Everything is computed from the raw events on every call, so a change to the
config (thresholds, client rules, task patterns) applies to past data as well.
"""

import bisect
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import db, timeline


@dataclass(frozen=True)
class Key:
    session_id: str
    client: Optional[str]
    billable: bool
    project: Optional[str]
    branch: Optional[str]
    task: Optional[str]


class _TaskLog:
    """A session's task assignments, (effective_from, task, branch) in time order."""

    def __init__(self, rows):
        self.rows = rows
        self.times = [r[0] for r in rows]

    def at(self, ts):
        """The assignment in effect at ts, or None. At the same moment the latest wins."""
        i = bisect.bisect_right(self.times, ts)
        return self.rows[i - 1] if i else None

    def changes_between(self, start, end):
        """Moments strictly inside (start, end) where the assignment changes."""
        return sorted(set(self.times[bisect.bisect_right(self.times, start):bisect.bisect_left(self.times, end)]))


def _task(cfg, row, log, at, client=None):
    """The task for time at `at` that hangs on event `row`.

    A confirmed assignment wins, with one exception: on a branch that carries
    its own task number, when the assignment was made on a different branch.
    Checking out feature/12345 means working on 12345; coming back, or moving
    to a branch without a number, the confirmed task applies again.
    """
    branch_task = cfg.qualify(client, cfg.branch_task(row["branch"]))
    hit = log.at(at)
    if hit is None:
        return branch_task
    _, task, assigned_on = hit
    if assigned_on is not None and row["branch"] != assigned_on and branch_task is not None:
        return branch_task
    return task


def _key(cfg, session_id, row, project_dir, log, at=None):
    client, billable = cfg.classify(row["cwd"])
    if client is None:
        # The agent may have cd'd somewhere unrelated (/tmp); the session
        # still belongs to the project it was started in.
        client, billable = cfg.classify(project_dir)
    task = _task(cfg, row, log, row["ts"] if at is None else at, client)
    return Key(session_id, client, billable, row["project"] or project_dir, row["branch"], task)


def intervals(conn, cfg, start, end):
    """Credited, labelled intervals [(start, end, Key)] clipped to [start, end)."""
    margin = cfg.tool_cap + cfg.idle
    by_session = defaultdict(list)
    for row in conn.execute("SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY session_id, ts, id",
                            (start - margin, end + margin)):
        by_session[row["session_id"]].append(row)
    if not by_session:
        return []

    project_dirs = {}
    sids = list(by_session)
    for i in range(0, len(sids), 500):
        chunk = sids[i:i + 500]
        for r in conn.execute(f"SELECT session_id, project_dir FROM sessions "
                              f"WHERE session_id IN ({', '.join('?' * len(chunk))})", chunk):
            project_dirs[r["session_id"]] = r["project_dir"]
    logs = db.assignments(conn, sids)

    out = []
    for sid, rows in by_session.items():
        log = _TaskLog(logs.get(sid, []))
        events = [timeline.Event(r["ts"], r["kind"], r["tool"], r["tool_use_id"], r) for r in rows]
        spans = []
        for s, e, ev in timeline.credit_session(events, cfg.idle, cfg.tool_cap):
            s, e = max(s, start), min(e, end)
            if s >= e:
                continue
            # A task set in the middle of an interval counts from that moment,
            # not from the next event.
            cuts = [s] + log.changes_between(s, e) + [e]
            for p0, p1 in zip(cuts, cuts[1:]):
                spans.append((p0, p1, _key(cfg, sid, ev.ref, project_dirs.get(sid), log, at=p0)))
        out.extend(timeline.merge(spans))
    out.sort(key=lambda iv: iv[0])
    return out


def _billable(key):
    return key.billable


def allocate(conn, cfg, start, end, mode=None):
    """Seconds per (Key, local day) between start and end.

    In split mode billable sessions take the clock first: your own projects
    running alongside never reduce a client's hours.
    """
    return timeline.allocate(intervals(conn, cfg, start, end), mode or cfg.overlap, priority=_billable)


def breakdown(conn, cfg, session_id, start=None, end=None):
    """How one session's time was counted: work blocks and the breaks between them.

    `seconds` of a work block is the session's own credited time; `counted` is
    what is left after sharing the clock with parallel sessions, which is what
    reports bill in split mode. LookupError for an unknown session.
    """
    sess = db.session(conn, session_id)
    if sess is None:
        raise LookupError(f"no session {session_id}")
    start = sess["first_ts"] if start is None else start
    end = sess["last_ts"] + cfg.tool_cap + cfg.idle if end is None else end

    keyed, mine = [], []
    for s, e, k in intervals(conn, cfg, start, end):
        if k.session_id == session_id:
            keyed.append((s, e, ("me", len(mine), k.billable)))
            mine.append((s, e, k))
        else:
            keyed.append((s, e, ("other", k.session_id, k.billable)))
    shares = defaultdict(float)
    for (key, _day), secs in timeline.allocate(keyed, cfg.overlap, priority=lambda key: key[2]).items():
        if key[0] == "me":
            shares[key[1]] += secs

    blocks = []
    for i, (s, e, k) in enumerate(mine):
        last = blocks[-1] if blocks else None
        if last and s > last["end"]:
            closed = conn.execute("SELECT 1 FROM events WHERE session_id = ? AND kind = 'session_end' "
                                  "AND ts >= ? AND ts <= ? LIMIT 1", (session_id, last["end"] - 1, s)).fetchone()
            minutes = round((s - last["end"]) / 60)
            blocks.append({"kind": "break", "start": last["end"], "end": s, "seconds": s - last["end"],
                           "counted": 0.0, "note": "session closed" if closed
                           else f"no agent activity for {minutes} min"})
            last = blocks[-1]
        if last and last["kind"] == "work" and last["end"] == s and (last["client"], last["task"]) == (k.client, k.task):
            last["end"] = e
            last["seconds"] += e - s
            last["counted"] += shares[i]
        else:
            blocks.append({"kind": "work", "start": s, "end": e, "seconds": e - s, "counted": shares[i],
                           "task": k.task, "client": k.client, "billable": k.billable, "branch": k.branch,
                           "project": k.project})

    work = [b for b in blocks if b["kind"] == "work"]
    client, billable = cfg.classify(sess["project_dir"])
    if work:
        client, billable = work[0]["client"], work[0]["billable"]
    return {
        "session": {"session_id": session_id, "short": session_id[:8], "title": sess["title"],
                    "account": sess["account"], "project_dir": sess["project_dir"], "client": client,
                    "billable": billable, "first": sess["first_ts"], "last": sess["last_ts"]},
        "blocks": blocks,
        "totals": {"credited": sum(b["seconds"] for b in work), "counted": sum(b["counted"] for b in work),
                   "breaks": sum(b["seconds"] for b in blocks if b["kind"] == "break")},
        "overlap": cfg.overlap,
    }


def local_midnight(ts):
    return datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def current(conn, cfg, session_id, now=None):
    """The session's Key right now: its latest event, with the task in effect now.

    None when the session has no events yet.
    """
    sess = db.session(conn, session_id)
    last = conn.execute("SELECT * FROM events WHERE session_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
                        (session_id,)).fetchone()
    if last is None:
        return None
    log = _TaskLog(db.assignments(conn, [session_id])[session_id])
    return _key(cfg, session_id, last, sess["project_dir"] if sess else None, log,
                at=now or time.time())


def today(conn, cfg, session_id, now=None):
    """What the status line shows: the session's task and today's time on it.

    With a task the time covers that task across every session today; without
    one it covers the session's project.
    """
    now = now or time.time()
    cur = current(conn, cfg, session_id, now)
    if cur is None:
        sess = db.session(conn, session_id)
        project_dir = sess["project_dir"] if sess else None
        client, billable = cfg.classify(project_dir)
        return {"session_id": session_id, "client": client, "billable": billable, "project": project_dir,
                "branch": None, "task": None, "title": None, "scope": "project", "seconds": 0.0,
                "updated": now}

    alloc = allocate(conn, cfg, local_midnight(now), now)
    if cur.task:
        scope = "task"
        seconds = sum(v for (k, _), v in alloc.items() if k.client == cur.client and k.task == cur.task)
    else:
        scope = "project"
        seconds = sum(v for (k, _), v in alloc.items() if k.project == cur.project)
    title = None
    if cur.task:
        row = conn.execute("SELECT title FROM tasks WHERE task = ?", (cur.task,)).fetchone()
        title = row["title"] if row else None
    return {"session_id": session_id, "client": cur.client, "billable": cur.billable,
            "project": cur.project, "branch": cur.branch, "task": cur.task, "title": title,
            "scope": scope, "seconds": seconds, "updated": now}
