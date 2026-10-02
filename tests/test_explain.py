import io
import os
import unittest

from tracker import cli, config, db, ledger
from tests.helpers import IsolatedTestCase, local_ts

HOME = os.path.expanduser("~")
ACME = f"{HOME}/dev/agency/acme/storefront"
Globex = f"{HOME}/dev/agency/Globex/globex-shop"
CONFIG = '[[rule]]\npath = "~/dev/agency/{client}/**"\n'


class BreakdownTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(CONFIG)
        self.cfg = config.load()
        self.conn = db.connect()
        self.t0 = local_ts("2026-09-15 10:00")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def add(self, sid, offset, kind, cwd=ACME, branch="main"):
        db.record_event(self.conn, ts=self.t0 + offset, session_id=sid, kind=kind, cwd=cwd, project=cwd,
                        branch=branch)
        db.touch_session(self.conn, sid, self.t0 + offset, project_dir=cwd)

    def blocks(self, sid="s1"):
        b = ledger.breakdown(self.conn, self.cfg, sid)
        return [(x["kind"], x["start"] - self.t0, x["end"] - self.t0, x.get("task"), round(x["counted"]))
                for x in b["blocks"]]

    def test_work_and_breaks_in_order_with_tasks(self):
        self.add("s1", 0, "prompt")
        self.add("s1", 600, "stop")
        self.add("s1", 2400, "prompt")                   # 30 min later: a break
        self.add("s1", 2700, "stop")
        db.add_assignment(self.conn, "s1", "SHOP-42", self.t0 + 2400, "cli")
        self.assertEqual(self.blocks(), [("work", 0, 600, None, 600),
                                         ("break", 600, 2400, None, 0),
                                         ("work", 2400, 2700, "SHOP-42", 300)])

    def test_parallel_session_halves_what_is_counted(self):
        self.add("s1", 0, "prompt")
        self.add("s1", 600, "stop")
        self.add("s2", 300, "prompt", cwd=Globex)
        self.add("s2", 900, "stop", cwd=Globex)
        b = ledger.breakdown(self.conn, self.cfg, "s1")
        self.assertEqual((round(b["totals"]["credited"]), round(b["totals"]["counted"])), (600, 450))

    def test_cli_prints_the_breakdown(self):
        self.add("s1abcdef", 0, "prompt")
        self.add("s1abcdef", 600, "stop")
        out = io.StringIO()
        self.assertEqual(cli.main(["explain", "--session", "s1ab"], now=self.t0 + 3600, out=out), 0)
        text = out.getvalue()
        self.assertIn("10:00–10:10", text)
        self.assertIn("0:10", text)


if __name__ == "__main__":
    unittest.main()
