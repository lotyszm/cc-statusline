"""Counted task time: days kept in the database always agree with counting from the first event."""

import os
import random
import sqlite3
from unittest import mock

from tracker import config, db, ledger, paths, tasktime, worklist
from tests.helpers import IsolatedTestCase, local_ts

HOME = os.path.expanduser("~")
ACME = f"{HOME}/dev/agency/acme/storefront"

RULES = """
[[rule]]
path = "~/dev/agency/{client}/**"

[[rule]]
path = "~/dev/ai/**"
client = "own"
billable = false
"""

BRANCH = "feature/12345-rounding"        # task 12345


def _ts(when):
    return local_ts(when) if isinstance(when, str) else when


class TaskTimeTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(RULES)
        self.cfg = config.load()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def event(self, sid, when, kind, branch=BRANCH, cwd=ACME, project_dir=ACME, **extra):
        ts = _ts(when)
        db.record_event(self.conn, ts=ts, session_id=sid, kind=kind, cwd=cwd, project=cwd, branch=branch, **extra)
        db.touch_session(self.conn, sid, ts, project_dir=project_dir)

    def work(self, sid, start, minutes, **kw):
        """Steady work from `start` for `minutes`, an event every five minutes."""
        t = local_ts(start)
        for m in range(0, minutes, 5):
            self.event(sid, t + m * 60, "prompt", **kw)
        self.event(sid, t + minutes * 60, "stop", **kw)

    def direct(self, now):
        """{(task, session): seconds} counted from the first event, as before the cache."""
        out = {}
        for (key, _day), secs in ledger.allocate(self.conn, self.cfg, 0, _ts(now)).items():
            if key.task:
                out[(key.task, key.session_id)] = out.get((key.task, key.session_id), 0.0) + secs
        return out

    def assertSameTime(self, got, want, msg=None):
        self.assertEqual(set(got), set(want), msg)
        for k in want:
            self.assertAlmostEqual(got[k], want[k], places=6, msg=msg)

    def check(self, now):
        """The counted time at `now`, after checking it against counting from the first event."""
        got = tasktime.seconds(self.conn, self.cfg, _ts(now))
        self.assertSameTime(got, self.direct(now))
        return got

    def starts(self, now):
        """Where one request at `now` started counting."""
        with mock.patch.object(ledger, "allocate", wraps=ledger.allocate) as spy:
            tasktime.seconds(self.conn, self.cfg, _ts(now))
        return [c.args[2] for c in spy.call_args_list]


class KeptDaysTest(TaskTimeTestCase):
    def test_a_second_request_counts_only_today(self):
        self.work("s1", "2026-09-14 10:00", 30)
        self.work("s1", "2026-09-15 10:00", 20)
        self.assertSameTime(self.check("2026-09-16 12:00"), {("12345", "s1"): 3000.0})
        self.assertEqual(self.starts("2026-09-16 12:30"), [local_ts("2026-09-16 00:00")])

    def test_today_is_never_kept(self):
        self.work("s1", "2026-09-16 10:00", 30)
        self.assertSameTime(self.check("2026-09-16 10:15"), {("12345", "s1"): 900.0})
        self.assertSameTime(self.check("2026-09-16 11:00"), {("12345", "s1"): 1800.0})

    def test_yesterday_is_kept_once_the_date_changes(self):
        self.work("s1", "2026-09-15 10:00", 30)
        self.check("2026-09-15 12:00")
        self.work("s1", "2026-09-15 14:00", 30)
        self.assertSameTime(self.check("2026-09-16 09:00"), {("12345", "s1"): 3600.0})
        self.assertEqual(self.starts("2026-09-16 09:05"), [local_ts("2026-09-16 00:00")])

    def test_an_earlier_now_counts_from_the_first_event_and_leaves_the_kept_days(self):
        self.work("s1", "2026-09-14 10:00", 30)
        self.work("s1", "2026-09-15 10:00", 30)
        self.check("2026-09-16 12:00")
        self.assertSameTime(self.check("2026-09-15 10:15"), {("12345", "s1"): 2700.0})
        self.assertEqual(self.starts("2026-09-16 12:05"), [local_ts("2026-09-16 00:00")])

    def test_a_read_only_database_still_counts(self):
        self.work("s1", "2026-09-15 10:00", 30)
        ro = sqlite3.connect(f"file:{paths.db_path()}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            self.assertSameTime(tasktime.seconds(ro, self.cfg, local_ts("2026-09-16 12:00")),
                                {("12345", "s1"): 1800.0})
        finally:
            ro.close()


class PastChangesTest(TaskTimeTestCase):
    """What changes a day already kept, and is counted again because of it."""

    def test_an_import_into_a_past_day(self):
        self.work("s1", "2026-09-15 10:00", 30)
        self.check("2026-09-16 12:00")
        self.work("s2", "2026-09-14 09:00", 40)
        self.assertSameTime(self.check("2026-09-16 12:05"), {("12345", "s1"): 1800.0, ("12345", "s2"): 2400.0})

    def test_a_session_that_goes_on_after_midnight(self):
        self.event("s1", "2026-09-15 23:40", "prompt")
        self.event("s1", "2026-09-15 23:55", "prompt")
        self.assertSameTime(self.check("2026-09-16 00:02"), {("12345", "s1"): 900.0})
        self.event("s1", "2026-09-16 00:05", "prompt")
        # 23:55 to 00:05 counts now: five of those minutes on the 15th.
        self.assertSameTime(self.check("2026-09-16 00:10"), {("12345", "s1"): 1500.0})

    def test_a_tool_that_ends_after_midnight_counts_from_its_start(self):
        self.event("s1", "2026-09-15 23:40", "prompt")
        self.event("s1", "2026-09-15 23:50", "tool_start", tool="Bash", tool_use_id="u1")
        self.assertSameTime(self.check("2026-09-16 00:30"), {("12345", "s1"): 600.0})
        self.event("s1", "2026-09-16 01:30", "tool_end", tool="Bash", tool_use_id="u1")
        # The running tool counts for an hour from 23:50: ten minutes of it on the 15th.
        self.assertSameTime(self.check("2026-09-16 02:00"), {("12345", "s1"): 4200.0})

    def test_a_tool_end_reaches_back_to_its_start_across_later_events(self):
        self.event("s1", "2026-09-15 23:30", "tool_start", tool="Agent", tool_use_id="u1")
        self.event("s1", "2026-09-15 23:31", "prompt")
        self.event("s1", "2026-09-15 23:59", "prompt")          # 28 minutes later: a break, unless u1 runs
        self.event("s1", "2026-09-16 00:10", "prompt")
        self.assertSameTime(self.check("2026-09-16 00:20"), {("12345", "s1"): 720.0})
        self.event("s1", "2026-09-16 00:40", "tool_end", tool="Agent", tool_use_id="u1")
        # 1 + 28 + 11 + 20 minutes: u1 ran from 23:30 and counts until 00:30.
        self.assertSameTime(self.check("2026-09-16 01:00"), {("12345", "s1"): 3600.0})

    def test_an_event_recorded_ahead_of_the_clock(self):
        self.event("s1", "2026-09-15 23:50", "tool_start", tool="Bash", tool_use_id="u1")
        self.event("s1", "2026-09-17 09:00", "tool_end", tool="Bash", tool_use_id="u1")
        self.assertSameTime(self.check("2026-09-16 12:00"), {})
        self.assertSameTime(self.check("2026-09-17 10:00"), {("12345", "s1"): 3600.0})

    def test_an_assignment_back_in_time(self):
        self.work("s1", "2026-09-15 10:00", 30, branch="main")
        self.assertSameTime(self.check("2026-09-16 12:00"), {})
        db.add_assignment(self.conn, "s1", "SHOP-42", local_ts("2026-09-15 10:00"), "manual")
        self.assertSameTime(self.check("2026-09-16 12:05"), {("SHOP-42", "s1"): 1800.0})

    def test_a_session_that_learns_its_project_later(self):
        self.write_config(RULES + "\n[tasks]\nnamespace = true\n")
        self.cfg = config.load()
        self.work("s1", "2026-09-15 10:00", 30, cwd="/tmp/elsewhere", project_dir=None)
        self.assertSameTime(self.check("2026-09-16 12:00"), {("12345", "s1"): 1800.0})
        db.touch_session(self.conn, "s1", local_ts("2026-09-15 10:00"), project_dir=ACME)
        self.assertSameTime(self.check("2026-09-16 12:05"), {("acme:12345", "s1"): 1800.0})

    def test_an_assignment_rewritten_in_place(self):
        self.work("s1", "2026-09-15 10:00", 30, branch="main")
        db.add_assignment(self.conn, "s1", "SHOP-42", local_ts("2026-09-15 10:00"), "manual")
        self.assertSameTime(self.check("2026-09-16 12:00"), {("SHOP-42", "s1"): 1800.0})
        self.conn.execute("UPDATE assignments SET task = 'SHOP-43'")
        self.assertSameTime(self.check("2026-09-16 12:05"), {("SHOP-43", "s1"): 1800.0})

    def test_a_ticket_put_on_a_work_list_task(self):
        self.conn.execute("INSERT INTO projects (slug, client, created_at) VALUES ('storefront', 'acme', 0)")
        self.work("s1", "2026-09-15 10:00", 30, branch="feature/SHOP-42-checkout")
        self.assertSameTime(self.check("2026-09-16 12:00"), {("SHOP-42", "s1"): 1800.0})
        key = worklist.add(self.conn, worklist.project(self.conn, "storefront"), "Checkout", ticket="SHOP-42")
        self.assertSameTime(self.check("2026-09-16 12:05"), {(key, "s1"): 1800.0})

    def test_a_new_client_on_the_work_list(self):
        own = f"{HOME}/dev/ai/statusline"
        self.work("s1", "2026-09-15 10:00", 30, cwd=own, branch="main")
        db.add_assignment(self.conn, "s1", "globex:SHOP-1", local_ts("2026-09-15 10:00"), "manual")
        self.work("s2", "2026-09-15 10:00", 30, cwd=own)
        # Two own sessions at once share the clock.
        self.assertSameTime(self.check("2026-09-16 12:00"), {("globex:SHOP-1", "s1"): 900.0, ("12345", "s2"): 900.0})
        self.conn.execute("INSERT INTO projects (slug, client, created_at) VALUES ('globex-shop', 'globex', 0)")
        # globex is a billable client now, and its task takes the clock first.
        self.assertSameTime(self.check("2026-09-16 12:05"), {("globex:SHOP-1", "s1"): 1800.0})

    def test_a_config_change(self):
        self.event("s1", "2026-09-15 10:00", "prompt")
        self.event("s1", "2026-09-15 10:20", "stop")            # a break at 15 idle minutes
        self.assertSameTime(self.check("2026-09-16 12:00"), {})
        self.write_config("idle_minutes = 30\n" + RULES)
        self.cfg = config.load()
        self.assertSameTime(self.check("2026-09-16 12:05"), {("12345", "s1"): 1200.0})

    def test_a_moved_task(self):
        for slug in ("storefront", "backoffice"):
            self.conn.execute("INSERT INTO projects (slug, client, created_at) VALUES (?, 'acme', 0)", (slug,))
        key = worklist.add(self.conn, worklist.project(self.conn, "storefront"), "Checkout")
        self.work("s1", "2026-09-15 10:00", 30, branch="main")
        db.add_assignment(self.conn, "s1", key, local_ts("2026-09-15 10:00"), "worklist")
        self.assertSameTime(self.check("2026-09-16 12:00"), {(key, "s1"): 1800.0})
        new = worklist.move(self.conn, key, "backoffice")
        self.assertSameTime(self.check("2026-09-16 12:05"), {(new, "s1"): 1800.0})


class RandomHistoryTest(TaskTimeTestCase):
    def test_kept_days_always_match_counting_from_the_first_event(self):
        rng = random.Random(6)
        first = local_ts("2026-09-10 00:00")
        branches = ("main", BRANCH, "feature/SHOP-7-banner", "feature/48302/fix")
        kinds = ("prompt", "prompt", "tool_start", "waiting", "stop", "session_end", "subagent_stop")
        now, tools = first + 6 * 3600, 0
        for step in range(120):
            for _ in range(rng.randint(1, 12)):
                sid, branch = f"s{rng.randint(1, 4)}", rng.choice(branches)
                where = rng.random()
                if where < 0.75:
                    ts = now - rng.uniform(0, 3 * 3600)
                elif where < 0.95:
                    ts = first + rng.uniform(0, now - first)          # an import into the past
                else:
                    ts = now + rng.uniform(0, 6 * 3600)               # ahead of the clock
                kind = rng.choice(kinds)
                if kind != "tool_start":
                    self.event(sid, ts, kind, branch=branch)
                    continue
                tools += 1
                self.event(sid, ts, kind, branch=branch, tool="Bash", tool_use_id=f"u{tools}")
                if rng.random() < 0.8:
                    self.event(sid, ts + rng.choice((30, 600, 5400, 30000)) * rng.random(), "tool_end",
                               branch=branch, tool="Bash", tool_use_id=f"u{tools}")
            if rng.random() < 0.1:
                db.add_assignment(self.conn, f"s{rng.randint(1, 4)}", rng.choice(("SHOP-9", None, "12345")),
                                  first + rng.uniform(0, now - first), "manual")
            if step == 70:
                self.write_config("idle_minutes = 25\ntool_cap_minutes = 40\n" + RULES)
                self.cfg = config.load()
            now += rng.uniform(0, 5 * 3600)
            self.assertSameTime(tasktime.seconds(self.conn, self.cfg, now), self.direct(now), f"step {step}")
