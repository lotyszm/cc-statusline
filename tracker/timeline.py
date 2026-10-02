"""Turns a session's events into credited work intervals.

The rules, in order, for each pair of consecutive events a -> b:

1. After `session_end` nothing is credited until the session comes back.
2. A gap no longer than the idle threshold is work: the agent was busy or you
   were reading and typing.
3. A longer gap is a break, unless a tool started earlier is still running
   (tests, a build, a subagent). Then the gap counts, but each tool for at most
   `tool_cap` seconds. A tool that waits for you (a question, a plan approval)
   or one stuck on a permission prompt does not keep the session busy.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

# Tools whose running time is spent waiting for the user, not working.
INTERACTIVE_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

# After these the agent is no longer running a tool on your behalf.
_IDLE_KINDS = frozenset({"waiting", "stop", "session_end"})


@dataclass
class Event:
    ts: float
    kind: str
    tool: Optional[str] = None
    tool_use_id: Optional[str] = None
    ref: Any = None


def credit_session(events, idle, tool_cap):
    """Work intervals [(start, end, event)] for one session, events sorted by ts.

    Each interval begins at an event and carries it, so the caller can label
    the time with that event's project, branch and task.
    """
    ends = {}
    for e in events:
        if e.kind == "tool_end" and e.tool_use_id and e.tool_use_id not in ends:
            ends[e.tool_use_id] = e.ts

    running = {}                    # tool_use_id -> start, for tools still working
    out = []
    for a, b in zip(events, events[1:]):
        if a.kind == "tool_start":
            # An end recorded before the start comes from two hooks racing; it
            # says nothing about the tool running afterwards.
            if a.tool not in INTERACTIVE_TOOLS and ends.get(a.tool_use_id, -1.0) >= a.ts:
                running[a.tool_use_id] = a.ts
        elif a.kind == "tool_end":
            running.pop(a.tool_use_id, None)
        elif a.kind in _IDLE_KINDS:
            running.clear()

        gap = b.ts - a.ts
        if gap <= 0 or a.kind == "session_end":
            continue
        if gap <= idle:
            credit = gap
        elif running:
            credit = min(gap, max(running.values()) + tool_cap - a.ts)
        else:
            continue
        if credit > 0:
            out.append((a.ts, a.ts + credit, a))
    return out


def merge(intervals):
    """Join intervals that touch and share a key; input sorted by start."""
    out = []
    for start, end, key in intervals:
        if out and out[-1][2] == key and out[-1][1] == start:
            out[-1] = (out[-1][0], end, key)
        else:
            out.append((start, end, key))
    return out


def _day_bounds(ts):
    """Local date of ts and the timestamp of the following local midnight."""
    d = datetime.fromtimestamp(ts).date()
    nxt = datetime.combine(d + timedelta(days=1), datetime.min.time()).timestamp()
    return d.isoformat(), nxt


def allocate(intervals, mode, priority=None):
    """Seconds per (key, local day) for intervals [(start, end, key)].

    mode "full" counts every interval completely. Mode "split" shares time in
    which several sessions were active: with n of them running, each gets 1/n,
    so the totals add up to real clock time. With `priority` (a predicate on
    keys), sessions it accepts take the clock first and share it among
    themselves; the others only get time when none of those is running. That
    is how client work keeps its hours while your own projects run alongside.
    """
    pieces = []
    for start, end, key in intervals:
        while start < end:
            day, midnight = _day_bounds(start)
            stop = min(end, midnight)
            pieces.append((start, stop, key, day))
            start = stop

    out = defaultdict(float)
    if mode == "full":
        for start, stop, key, day in pieces:
            out[(key, day)] += stop - start
        return dict(out)

    starts, stops = defaultdict(list), defaultdict(list)
    for i, (start, stop, _, _) in enumerate(pieces):
        starts[start].append(i)
        stops[stop].append(i)
    first = [bool(priority(p[2])) for p in pieces] if priority else None
    active, prev = set(), None
    for t in sorted(set(starts) | set(stops)):
        if active:
            sharing = [i for i in active if first[i]] if first else None
            sharing = sharing or active
            share = (t - prev) / len(sharing)
            for i in sharing:
                out[(pieces[i][2], pieces[i][3])] += share
        active.difference_update(stops.get(t, ()))
        active.update(starts.get(t, ()))
        prev = t
    return dict(out)
