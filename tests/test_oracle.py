"""Brute-force cross-check of the time ledger.

Random sessions with integer timestamps are counted second by second by a
deliberately naive implementation of the rules, written without timeline.py's
incremental state, then compared with ledger.allocate in both overlap modes.
A difference anywhere (crediting, clipping, merging, day cuts, overlap shares,
task attribution) fails the test with the first mismatching key.
"""

import bisect
import os
import random
import unittest
from collections import defaultdict
from datetime import datetime, timedelta

from tracker import config, db, ledger
from tests.helpers import IsolatedTestCase, local_ts

HOME = os.path.expanduser("~")
CWDS = [f"{HOME}/dev/agency/acme/shop", f"{HOME}/dev/agency/Globex/store", f"{HOME}/dev/ai/tool"]
BRANCHES = ["main", "feature/12345/fix", "feat/SHOP-42-cart", "feature/23456/x"]
TASKS = ["A-1", "B-2", None]
INTERACTIVE = {"AskUserQuestion", "ExitPlanMode"}
IDLE_KINDS = {"waiting", "stop", "session_end"}
CONFIG = """
[[rule]]
path = "~/dev/agency/{client}/**"

[[rule]]
path = "~/dev/ai/**"
client = "own"
billable = false
"""


class Ev:
    def __init__(self, ts, kind, tool, tool_use_id, cwd, branch):
        self.ts, self.kind, self.tool, self.tool_use_id = ts, kind, tool, tool_use_id
        self.cwd, self.branch = cwd, branch


def generate(rng, t0, n_sessions):
    """[(session_id, project_dir, [Ev], [(effective_from, task, branch)])]"""
    out = []
    for s in range(n_sessions):
        sid, home_cwd = f"s{s}", rng.choice(CWDS)
        t, branch, open_tools, events = t0 + rng.randint(0, 3600), rng.choice(BRANCHES), [], []
        for n in range(rng.randint(5, 45)):
            r = rng.random()
            t += rng.randint(1, 600) if r < 0.6 else rng.randint(600, 1200) if r < 0.85 else rng.randint(1200, 5000)
            if rng.random() < 0.1:
                branch = rng.choice(BRANCHES)
            cwd = "/tmp/elsewhere" if rng.random() < 0.05 else home_cwd
            k = rng.random()
            if k < 0.25 and len(open_tools) < 2:
                tool = "AskUserQuestion" if rng.random() < 0.15 else rng.choice(["Bash", "Agent", "Read"])
                tid = f"{sid}-t{n}"
                open_tools.append((tid, tool))
                events.append(Ev(t, "tool_start", tool, tid, cwd, branch))
            elif k < 0.45 and open_tools:
                tid, tool = open_tools.pop(rng.randrange(len(open_tools)))
                if rng.random() < 0.9:                       # some tools never report an end
                    events.append(Ev(t, "tool_end", tool, tid, cwd, branch))
            else:
                kind = rng.choice(["prompt", "stop", "waiting", "subagent_stop", "prompt", "stop", "session_end"])
                events.append(Ev(t, kind, None, None, cwd, branch))
        first, last = events[0].ts, events[-1].ts
        assigns = sorted(((rng.randint(first, last), rng.choice(TASKS), rng.choice(BRANCHES + [None]))
                          for _ in range(rng.choice([0, 0, 1, 2, 3]))), key=lambda a: a[0])
        out.append((sid, home_cwd, events, assigns))
    return out


def credited_seconds(events, idle, cap):
    """{second: index of the event the second hangs on}, rules applied naively."""
    idle, cap = int(idle), int(cap)
    out = {}
    first_end = {}
    for i, e in enumerate(events):
        if e.kind == "tool_end" and e.tool_use_id not in first_end:
            first_end[e.tool_use_id] = i
    for i in range(len(events) - 1):
        a, b = events[i], events[i + 1]
        gap = b.ts - a.ts
        if a.kind == "session_end" or gap <= 0:
            continue
        limit = b.ts if gap <= idle else a.ts
        if gap > idle:
            for j in range(i + 1):
                st = events[j]
                if st.kind != "tool_start" or st.tool in INTERACTIVE:
                    continue
                end = first_end.get(st.tool_use_id)
                if end is None or events[end].ts < st.ts:
                    continue                                 # never ended, or recorded out of order
                if end <= i:
                    continue                                 # already finished at a
                ends_seen = [k for k in range(j + 1, i + 1)
                             if events[k].kind == "tool_end" and events[k].tool_use_id == st.tool_use_id]
                if ends_seen:
                    continue
                if any(events[k].kind in IDLE_KINDS for k in range(j + 1, i + 1)):
                    continue                                 # a wait or the end of a turn stopped it
                limit = max(limit, min(b.ts, st.ts + cap))
        for t in range(a.ts, limit):
            out[t] = i
    return out


def oracle(sessions, cfg, start, end, mode, semantics):
    credited = {sid: credited_seconds(evs, cfg.idle, cfg.tool_cap) for sid, _, evs, _ in sessions}
    by_sid = {sid: (pdir, evs, assigns) for sid, pdir, evs, assigns in sessions}
    day_cache = {}

    def day(t):
        d = datetime.fromtimestamp(t).date()
        key = (d, t // 3600)
        if key not in day_cache:
            day_cache[key] = d.isoformat()
        return day_cache[key]

    def label(sid, t):
        pdir, evs, assigns = by_sid[sid]
        a = evs[credited[sid][t]]
        client, billable = cfg.classify(a.cwd)
        if client is None:
            client, billable = cfg.classify(pdir)
        at = a.ts if semantics == "event" else t
        times = [x[0] for x in assigns]
        i = bisect.bisect_right(times, at)
        branch_task = cfg.branch_task(a.branch)
        if i:
            _, task, abranch = assigns[i - 1]
            if semantics == "second" and abranch is not None and a.branch != abranch and branch_task is not None:
                task = branch_task
        else:
            task = branch_task
        return client, billable, task

    out = defaultdict(float)
    for t in range(start, end):
        active = [(sid, label(sid, t)) for sid in credited if t in credited[sid]]
        if mode == "split":
            # Billable sessions take the clock first; own work only gets the rest.
            active = [(sid, lab) for sid, lab in active if lab[1]] or active
        for sid, (client, _billable, task) in active:
            out[(sid, client, task, day(t))] += 1 / len(active) if mode == "split" else 1
    return {k: round(v, 6) for k, v in out.items() if round(v, 6)}


class OracleTestCase(IsolatedTestCase):
    semantics = "second"

    def setUp(self):
        super().setUp()
        self.write_config(CONFIG)
        self.cfg = config.load()

    def load(self, sessions, seed):
        conn = db.connect(os.path.join(self.tmp, f"oracle-{seed}.db"))   # fresh per seed
        for sid, pdir, evs, assigns in sessions:
            for e in evs:
                db.record_event(conn, ts=e.ts, session_id=sid, kind=e.kind, cwd=e.cwd, project=e.cwd,
                                branch=e.branch, tool=e.tool, tool_use_id=e.tool_use_id)
                db.touch_session(conn, sid, e.ts, project_dir=pdir)
            for eff, task, branch in assigns:
                db.add_assignment(conn, sid, task, eff, "test", branch=branch)
        return conn

    def compare(self, seed, n_sessions=6, clip=False):
        rng = random.Random(seed)
        t0 = int(local_ts("2026-09-15 21:00"))
        sessions = generate(rng, t0, n_sessions)
        conn = self.load(sessions, seed)
        lo = min(e.ts for _, _, evs, _ in sessions for e in evs)
        hi = max(e.ts for _, _, evs, _ in sessions for e in evs) + 1
        if clip:                                             # a range that cuts through the data
            lo, hi = lo + (hi - lo) // 3, hi - (hi - lo) // 4
        for mode in ("split", "full"):
            want = oracle(sessions, self.cfg, lo, hi, mode, self.semantics)
            summed = defaultdict(float)      # ledger keys also carry project and branch
            for (k, day), v in ledger.allocate(conn, self.cfg, lo, hi, mode).items():
                summed[(k.session_id, k.client, k.task, day)] += v
            got = {k: round(v, 6) for k, v in summed.items() if round(v, 6)}
            diff = (sorted(set(want) ^ set(got), key=str)
                    or sorted((k for k in want if abs(want[k] - got[k]) > 1e-4), key=str))
            self.assertFalse(diff, f"seed {seed} mode {mode}: first difference {diff[:3]} "
                                   f"want {[want.get(k) for k in diff[:3]]} got {[got.get(k) for k in diff[:3]]}")
        conn.close()


class OracleTest(OracleTestCase):
    def test_random_sessions_match_a_second_by_second_count(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                self.compare(seed)

    def test_a_range_cutting_through_sessions_matches_too(self):
        for seed in range(100, 106):
            with self.subTest(seed=seed):
                self.compare(seed, clip=True)


if __name__ == "__main__":
    unittest.main()
