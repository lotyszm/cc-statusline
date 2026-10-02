import contextlib
import csv
import io
import json
import os
import unittest

from tracker import cli, db, paths
from tests.helpers import IsolatedTestCase, local_ts

HOME = os.path.expanduser("~")
ACME = f"{HOME}/dev/agency/acme/storefront"
Globex = f"{HOME}/dev/agency/Globex/globex-shop"
OWN = f"{HOME}/dev/ai/statusline"

CONFIG = """
currency = "PLN"

[[rule]]
path = "~/dev/agency/{client}/**"

[[rule]]
path = "~/dev/ai/**"
client = "own"
billable = false

[clients.acme]
rate = 150
"""


class CliTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(CONFIG)
        self.conn = db.connect()
        self.t0 = local_ts("2026-09-15 10:00")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def busy(self, sid, start, end, cwd=ACME, branch="main"):
        for off, kind in ((start, "prompt"), (end, "stop")):
            db.record_event(self.conn, ts=self.t0 + off, session_id=sid, kind=kind, cwd=cwd,
                            project=cwd, branch=branch)
            db.touch_session(self.conn, sid, self.t0 + off, project_dir=cwd)

    def run_cli(self, *argv, now=None):
        out = io.StringIO()
        code = cli.main(list(argv), now=now or self.t0 + 7200, out=out)
        return code, out.getvalue()

    def efforts(self, sid):
        return [(r["task"], r["effective_from"], r["origin"]) for r in self.conn.execute(
            "SELECT * FROM assignments WHERE session_id = ? ORDER BY id", (sid,))]


class TaskSetTest(CliTestCase):
    def test_first_task_of_a_session_covers_it_from_the_start(self):
        self.busy("s1abcdef", 0, 600)
        code, _ = self.run_cli("task", "set", "SHOP-42", "--session", "s1abcdef", now=self.t0 + 900)
        self.assertEqual(code, 0)
        self.assertEqual(self.efforts("s1abcdef"), [("SHOP-42", self.t0, "cli")])

    def test_switching_from_a_branch_task_applies_from_now(self):
        self.busy("s1abcdef", 0, 600, branch="feature/12345/x")
        self.run_cli("task", "set", "SHOP-42", "--session", "s1abcdef", now=self.t0 + 900)
        self.assertEqual(self.efforts("s1abcdef"), [("SHOP-42", self.t0 + 900, "cli")])

    def test_since_takes_a_timestamp_or_a_local_time(self):
        self.busy("s1abcdef", 0, 600)
        self.run_cli("task", "set", "A-1", "--session", "s1abcdef", "--since", f"{self.t0 + 123.5}")
        self.run_cli("task", "set", "B-2", "--session", "s1abcdef", "--since", "2026-09-15 10:05")
        self.assertEqual([e[1] for e in self.efforts("s1abcdef")], [self.t0 + 123.5, self.t0 + 300])

    def test_session_prefix_is_enough(self):
        self.busy("s1abcdef", 0, 600)
        code, _ = self.run_cli("task", "set", "A-1", "--session", "s1ab")
        self.assertEqual((code, len(self.efforts("s1abcdef"))), (0, 1))

    def test_unknown_session_fails_without_writing(self):
        code, _ = self.run_cli("task", "set", "A-1", "--session", "nope")
        self.assertEqual(code, 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM assignments").fetchone()[0], 0)

    def test_title_is_stored_and_status_refreshed(self):
        self.busy("s1abcdef", 0, 600)
        self.run_cli("task", "set", "A-1", "--session", "s1abcdef", "--title", "Checkout fix")
        self.assertEqual(db.task_titles(self.conn), {"A-1": "Checkout fix"})
        with open(paths.status_dir() / "s1abcdef.json") as f:
            self.assertEqual(json.load(f)["task"], "A-1")

    def test_clear_marks_the_session_as_no_task(self):
        self.busy("s1abcdef", 0, 600, branch="feature/12345/x")
        self.run_cli("task", "clear", "--session", "s1abcdef", "--since", f"{self.t0}")
        self.assertEqual(self.efforts("s1abcdef"), [(None, self.t0, "cli")])


class ReportTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.busy("s1", 0, 600, branch="feature/12345/x")
        self.busy("s2", 1000, 1300, cwd=Globex)
        self.busy("s3", 2000, 2120, cwd=OWN)
        db.set_task_title(self.conn, "12345", "Hotfix pricing")

    def csv_rows(self, *extra):
        code, text = self.run_cli("report", "--from", "2026-09-15", "--to", "2026-09-15",
                                  "--format", "csv", *extra)
        self.assertEqual(code, 0)
        return list(csv.DictReader(io.StringIO(text)))

    def test_hours_per_client_and_task(self):
        got = [(r["client"], r["task"], r["title"], r["minutes"], r["hours"], r["amount"])
               for r in self.csv_rows()]
        self.assertEqual(got, [("acme", "12345", "Hotfix pricing", "10", "0.17", "25.00"),
                               ("Globex", "", "", "5", "0.08", ""),
                               ("own", "", "", "2", "0.03", "")])

    def test_client_filter(self):
        self.assertEqual([r["client"] for r in self.csv_rows("--client", "globex")], ["Globex"])

    def test_billable_only(self):
        self.assertEqual([r["client"] for r in self.csv_rows("--billable")], ["acme", "Globex"])

    def test_group_by_day(self):
        got = [(r["day"], r["client"], r["minutes"]) for r in self.csv_rows("--by", "day,client")]
        self.assertEqual(got, [("2026-09-15", "acme", "10"), ("2026-09-15", "Globex", "5"),
                               ("2026-09-15", "own", "2")])

    def test_range_outside_the_data_is_empty(self):
        code, text = self.run_cli("report", "--from", "2026-09-01", "--to", "2026-09-02", "--format", "csv")
        self.assertEqual(text.strip().splitlines()[1:], [])


class SessionsTest(CliTestCase):
    def test_unassigned_lists_billable_sessions_without_a_task(self):
        self.busy("aaaa1111", 0, 600, branch="feature/12345/x")
        self.busy("bbbb2222", 1000, 1300, cwd=Globex)
        self.busy("cccc3333", 2000, 2120, cwd=OWN)
        code, text = self.run_cli("sessions", "--from", "2026-09-15", "--to", "2026-09-15", "--unassigned")
        self.assertEqual(code, 0)
        self.assertIn("bbbb2222", text)
        self.assertNotIn("aaaa1111", text)
        self.assertNotIn("cccc3333", text)
        self.assertIn("assign with: cc-statusline task set", text)


class ParserTest(unittest.TestCase):
    def test_commands_run_as_cc_statusline(self):
        self.assertEqual(cli.build_parser().prog, "cc-statusline")

    def test_setup_is_left_to_statusline_py(self):
        # --install, --uninstall and --doctor are statusline.py flags.
        for command in ("install", "uninstall", "doctor"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cli.build_parser().parse_args([command])


if __name__ == "__main__":
    unittest.main()
