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


def _task(cfg, row, log, at, client=None, aliases=None):
    """The task for time at `at` that hangs on event `row`.

    A confirmed assignment wins, with one exception: on a branch that carries
    its own task number, when the assignment was made on a different branch.
    Checking out feature/12345 means working on 12345; coming back, or moving
    to a branch without a number, the confirmed task applies again.

    A ticket that a work-list task carries (CMS-706) stands for that task.
    """
    return db.alias_of(aliases, _raw_task(cfg, row, log, at, client))


def _raw_task(cfg, row, log, at, client):
    branch_task = cfg.qualify(client, cfg.branch_task(row["branch"]))
    hit = log.at(at)
    if hit is None:
        return branch_task
    _, task, assigned_on = hit
    if assigned_on is not None and row["branch"] != assigned_on and branch_task is not None:
        return branch_task
    return task


def _key(cfg, session_id, row, project_dir, log, at=None, aliases=None, clients=None):
    client, billable = cfg.classify(row["cwd"])
    if client is None:
        # The agent may have cd'd somewhere unrelated (/tmp); the session
        # still belongs to the project it was started in.
        client, billable = cfg.classify(project_dir)
    task = _task(cfg, row, log, row["ts"] if at is None else at, client, aliases)
    named = cfg.task_client(task, clients)
    if named is not None:
        client, billable = named
    return Key(session_id, client, billable, row["project"] or project_dir, row["branch"], task)


def intervals(conn, cfg, start, end, per_agent=None):
    """Credited, labelled intervals [(start, end, Key)] clipped to [start, end).

    With `per_agent` (the default in full overlap mode) a subagent that has a
    task of its own is a stream of its own, counted from its events and
    labelled with its task; with `subagents = "workers"` every subagent is,
    labelled with the session's task until it has one of its own. The
    session's stream is counted as always, from all of its events, but gives
    up the moments in which only such agents were at work while the session
    waited for them. A subagent without a task of its own otherwise keeps
    working on the session's, and within a session a task is never counted
    twice for the same moment. In split mode subagents are never separated:
    there every parallel stream would take a share of the clock.
    """
    if per_agent is None:
        per_agent = cfg.overlap == "full"
    workers = per_agent and cfg.subagents == "workers"
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
    agent_logs = defaultdict(dict)
    if per_agent:
        for (sid, agent_id), rows in db.agent_assignments(conn, sids).items():
            agent_logs[sid][agent_id] = _TaskLog(rows)
    aliases = db.task_aliases(conn)
    clients = cfg.client_flags(db.project_clients(conn))

    def raw(rows):
        events = [timeline.Event(r["ts"], r["kind"], r["tool"], r["tool_use_id"], r) for r in rows]
        for s, e, ev in timeline.credit_session(events, cfg.idle, cfg.tool_cap):
            s, e = max(s, start), min(e, end)
            if s < e:
                yield s, e, ev

    def labelled(sid, s, e, ev, log):
        # A task set in the middle of an interval counts from that moment,
        # not from the next event.
        cuts = [s] + log.changes_between(s, e) + [e]
        for p0, p1 in zip(cuts, cuts[1:]):
            yield p0, p1, _key(cfg, sid, ev.ref, project_dirs.get(sid), log, at=p0,
                               aliases=aliases, clients=clients)

    out = []
    for sid, rows in by_session.items():
        log = _TaskLog(logs.get(sid, []))
        session = timeline.merge([span for s, e, ev in raw(rows) for span in labelled(sid, s, e, ev, log)])
        agents = agent_logs.get(sid) or {}
        if not agents and not (workers and any(r["agent_id"] for r in rows)):
            out.extend(session)
            continue
        out.extend(_with_agents(sid, rows, session, agents, log, workers, raw, labelled))
    out.sort(key=lambda iv: iv[0])
    return out


def _working(ev):
    """Whether a session works after its own event: not once its turn has ended,
    nor while it waits for a foreground agent."""
    return ev.kind not in ("stop", "waiting") and not (ev.kind == "tool_start" and ev.tool == "Agent")


def _with_agents(sid, rows, session, agents, log, workers, raw, labelled):
    """A session's intervals when some of its subagents count apart from it.

    Apart are the agents with tasks of their own, from the moment they got
    them, and with `workers` every agent; each one's time goes to its task, or
    to the session's. The session keeps its time (`session`, counted from all
    its events as always) except where only agents apart were at work while it
    waited for them.
    """
    by_agent = defaultdict(list)
    for r in rows:
        by_agent[r["agent_id"]].append(r)
    busy = [(s, e) for s, e, ev in raw(by_agent.pop(None, [])) if _working(ev)]
    apart, streams = [], []
    for agent_id in sorted(by_agent):
        alog = agents.get(agent_id)
        if alog is None and not workers:
            busy += [(s, e) for s, e, _ in raw(by_agent[agent_id])]    # an agent on the session's task
            continue
        since = alog.times[0] if alog else float("inf")
        tasks = _TaskLog([r for r in log.rows if r[0] < since] + (alog.rows if alog else []))
        own = []
        for s, e, ev in raw(by_agent[agent_id]):
            for p0, p1, key in labelled(sid, s, e, ev, tasks):
                if p0 < since and not workers:
                    busy.append((p0, p1))           # before the agent got a task
                else:
                    apart.append((p0, p1))
                    own.append((p0, p1, key))
        streams.append(timeline.merge(own))
    busy = _normalize(busy)
    only_apart = [p for s, e in _normalize(apart) for p in _minus(s, e, busy)]
    rest = [(p0, p1, key) for s, e, key in session for p0, p1 in _minus(s, e, only_apart)]
    if workers:
        return rest + [iv for spans in streams for iv in spans]     # five agents on a task: five workers
    return _once_per_task([rest] + streams)


def _once_per_task(streams):
    """The streams' intervals with time a task already has from an earlier stream cut out.

    Two streams of one session on the same task at the same moment are one
    piece of work on it, not two.
    """
    covered = defaultdict(list)          # (client, task) -> sorted disjoint [(start, end)]
    out = []
    for spans in streams:
        taken = []
        for s, e, key in spans:
            ident = (key.client, key.task)
            for p0, p1 in _minus(s, e, covered[ident]):
                out.append((p0, p1, key))
                taken.append((ident, p0, p1))
        for ident, p0, p1 in taken:
            covered[ident] = _union(covered[ident], p0, p1)
    return out


def _minus(start, end, covered):
    """Parts of [start, end) outside the sorted disjoint intervals `covered`."""
    out, cur = [], start
    for s, e in covered[max(0, bisect.bisect_left(covered, (start,)) - 1):]:
        if e <= cur:
            continue
        if s >= end:
            break
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
        if cur >= end:
            break
    if cur < end:
        out.append((cur, end))
    return out


def _normalize(spans):
    """Sorted, disjoint union of [(start, end)]."""
    merged = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _union(covered, start, end):
    return _normalize(covered + [(start, end)])


def _billable(key):
    return key.billable


def allocate(conn, cfg, start, end, mode=None):
    """Seconds per (Key, local day) between start and end.

    In split mode billable sessions take the clock first: your own projects
    running alongside never reduce a client's hours.
    """
    mode = mode or cfg.overlap
    return timeline.allocate(intervals(conn, cfg, start, end, per_agent=mode == "full"), mode,
                             priority=_billable)


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
    reach = None                # where work so far ends: agents on tasks of their own overlap
    for i, (s, e, k) in enumerate(mine):
        last = blocks[-1] if blocks else None
        if last and s > reach:
            closed = conn.execute("SELECT 1 FROM events WHERE session_id = ? AND kind = 'session_end' "
                                  "AND ts >= ? AND ts <= ? LIMIT 1", (session_id, reach - 1, s)).fetchone()
            minutes = round((s - reach) / 60)
            blocks.append({"kind": "break", "start": reach, "end": s, "seconds": s - reach,
                           "counted": 0.0, "note": "session closed" if closed
                           else f"no agent activity for {minutes} min"})
            last = blocks[-1]
        reach = e if reach is None else max(reach, e)
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
                at=now or time.time(), aliases=db.task_aliases(conn),
                clients=cfg.client_flags(db.project_clients(conn)))


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
