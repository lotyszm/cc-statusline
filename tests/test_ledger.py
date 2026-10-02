import os
import unittest

from tracker import config, db, ledger
from tests.helpers import IsolatedTestCase, local_ts

HOME = os.path.expanduser("~")
ACME = f"{HOME}/dev/agency/acme/storefront"
Globex = f"{HOME}/dev/agency/Globex/globex-shop"
OWN = f"{HOME}/dev/ai/statusline"

RULES = """
[[rule]]
path = "~/dev/agency/{client}/**"

[[rule]]
path = "~/dev/ai/**"
client = "own"
billable = false
"""


class LedgerTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(RULES)
        self.cfg = config.load()
        self.conn = db.connect()
        self.t0 = local_ts("2026-09-15 10:00")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def add(self, sid, offset, kind, cwd=ACME, branch="main", **extra):
        ts = self.t0 + offset
        db.record_event(self.conn, ts=ts, session_id=sid, kind=kind, cwd=cwd, project=cwd,
                        branch=branch, **extra)
        db.touch_session(self.conn, sid, ts, project_dir=cwd)

    def busy(self, sid, start, end, **kw):
        """A session working steadily from start to end (offsets in seconds)."""
        self.add(sid, start, "prompt", **kw)
        for t in range(start + 300, end, 300):
            self.add(sid, t, "tool_start", tool="Bash", tool_use_id=f"{sid}{t}", **kw)
        self.add(sid, end, "stop", **kw)

    def totals(self, start=None, end=None, by=("client", "task"), mode="split"):
        start = self.t0 - 3600 if start is None else start
        end = self.t0 + 6 * 3600 if end is None else end
        out = {}
        for (key, _day), secs in ledger.allocate(self.conn, self.cfg, start, end, mode).items():
            k = tuple(getattr(key, f) for f in by)
            out[k] = out.get(k, 0) + secs
        return {k: round(v) for k, v in out.items()}


class AttributionTest(LedgerTestCase):
    def test_client_comes_from_rules_and_task_from_the_branch(self):
        self.busy("s1", 0, 600, branch="feature/12345/hotfix-pricing")
        self.assertEqual(self.totals(), {("acme", "12345"): 600})

    def test_billable_flag_follows_the_rule(self):
        self.busy("s1", 0, 600, cwd=OWN)
        self.assertEqual(self.totals(by=("client", "billable")), {("own", False): 600})

    def test_confirmed_task_applies_from_its_effective_time(self):
        self.busy("s1", 0, 300)
        self.busy("s1", 400, 700)
        db.add_assignment(self.conn, "s1", "SHOP-42", self.t0 + 400, "confirmed")
        self.assertEqual(self.totals(), {("acme", None): 400, ("acme", "SHOP-42"): 300})

    def test_explicit_no_task_overrides_the_branch(self):
        self.busy("s1", 0, 600, branch="feature/12345/hotfix-pricing")
        db.add_assignment(self.conn, "s1", None, self.t0, "manual")
        self.assertEqual(self.totals(), {("acme", None): 600})

    def test_latest_assignment_wins_at_the_same_moment(self):
        self.busy("s1", 0, 600)
        db.add_assignment(self.conn, "s1", "A-1", self.t0, "confirmed")
        db.add_assignment(self.conn, "s1", "B-2", self.t0, "manual")
        self.assertEqual(self.totals(), {("acme", "B-2"): 600})

    def test_a_task_set_inside_an_interval_counts_from_that_moment(self):
        self.add("s1", 0, "prompt")
        self.add("s1", 600, "stop")                       # one ten-minute interval
        db.add_assignment(self.conn, "s1", "SHOP-42", self.t0 + 240, "cli")
        self.assertEqual(self.totals(), {("acme", None): 240, ("acme", "SHOP-42"): 360})

    def test_a_numbered_branch_beats_a_task_confirmed_on_another_branch(self):
        self.busy("s1", 0, 600, branch="feat/checkout")
        db.add_assignment(self.conn, "s1", "SHOP-42", self.t0, "cli", branch="feat/checkout")
        self.busy("s1", 700, 1300, branch="feature/12345/hotfix")
        self.busy("s1", 1400, 2000, branch="main")      # no number: the confirmed task goes on
        self.assertEqual(self.totals(), {("acme", "SHOP-42"): 1300, ("acme", "12345"): 700})

    def test_a_task_confirmed_on_a_numbered_branch_wins_there(self):
        self.busy("s1", 0, 600, branch="feature/12345/hotfix")
        db.add_assignment(self.conn, "s1", "SHOP-42", self.t0, "cli", branch="feature/12345/hotfix")
        self.assertEqual(self.totals(), {("acme", "SHOP-42"): 600})

    def test_cwd_outside_any_rule_falls_back_to_the_session_project(self):
        db.touch_session(self.conn, "s1", self.t0, project_dir=ACME)
        self.add("s1", 0, "prompt", cwd="/tmp")
        self.add("s1", 600, "stop", cwd="/tmp")
        self.assertEqual(self.totals(), {("acme", None): 600})


class RangeAndOverlapTest(LedgerTestCase):
    def test_time_outside_the_range_is_clipped(self):
        self.busy("s1", 0, 600)
        got = self.totals(start=self.t0 + 100, end=self.t0 + 200)
        self.assertEqual(got, {("acme", None): 100})

    def test_parallel_clients_share_the_clock(self):
        self.busy("s1", 0, 3600)
        self.busy("s2", 1800, 5400, cwd=Globex)
        self.assertEqual(self.totals(by=("client",)), {("acme",): 2700, ("Globex",): 2700})

    def test_own_work_in_parallel_does_not_reduce_client_time(self):
        self.busy("s1", 0, 3600)                         # client
        self.busy("s2", 1800, 5400, cwd=OWN)             # own project, in parallel half the time
        self.assertEqual(self.totals(by=("client",)), {("acme",): 3600, ("own",): 1800})

    def test_full_mode_counts_each_client_completely(self):
        self.busy("s1", 0, 3600)
        self.busy("s2", 1800, 5400, cwd=Globex)
        self.assertEqual(self.totals(by=("client",), mode="full"), {("acme",): 3600, ("Globex",): 3600})


class TodayTest(LedgerTestCase):
    def test_scope_is_the_task_across_sessions(self):
        self.busy("s1", 0, 600, branch="feature/12345/x")
        self.busy("s2", 1000, 1300, branch="feature/12345/x")
        self.busy("s3", 2000, 2100, branch="feature/1111/y")
        got = ledger.today(self.conn, self.cfg, "s2", now=self.t0 + 3000)
        self.assertEqual((got["task"], got["scope"], round(got["seconds"])), ("12345", "task", 900))
        self.assertEqual((got["client"], got["billable"]), ("acme", True))

    def test_scope_falls_back_to_the_project_without_a_task(self):
        self.busy("s1", 0, 600)
        self.busy("s2", 1000, 1300, cwd=Globex)
        got = ledger.today(self.conn, self.cfg, "s1", now=self.t0 + 3000)
        self.assertEqual((got["task"], got["scope"], round(got["seconds"])), (None, "project", 600))


if __name__ == "__main__":
    unittest.main()
