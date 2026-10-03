"""Task descriptions and plans, and client-namespaced task keys."""

import contextlib
import io
import json
import os
import sqlite3
import unittest

from tracker import cli, config, dashboard, db, hook, ledger
from tests.helpers import IsolatedTestCase, local_ts

HOME = os.path.expanduser("~")
ACME = f"{HOME}/dev/agency/acme/storefront"

CONFIG = """
[[rule]]
path = "~/dev/agency/{client}/**"

[tasks]
namespace = true
"""

V2_TASKS = """
CREATE TABLE tasks (task TEXT PRIMARY KEY, title TEXT, updated_at REAL);
INSERT INTO tasks VALUES ('A-1', 'Checkout fix', 10);
PRAGMA user_version = 2;
"""


class TaskStoreTest(IsolatedTestCase):
    def test_a_version_2_database_keeps_its_titles_and_gains_details(self):
        path = os.path.join(self.tmp, "v2.db")
        raw = sqlite3.connect(path)
        raw.executescript(V2_TASKS)
        raw.close()
        conn = db.connect(path)
        db.set_task(conn, "A-1", 20, plan="Round the price once.")
        row = db.task_details(conn, "A-1")
        self.assertEqual((row["title"], row["plan"], row["updated_at"]), ("Checkout fix", "Round the price once.", 20))
        conn.close()

    def test_setting_a_title_keeps_the_description(self):
        conn = db.connect()
        db.set_task(conn, "A-1", description="Prices are off by a cent.", status="open")
        db.set_task_title(conn, "A-1", "Checkout fix")
        row = db.task_details(conn, "A-1")
        self.assertEqual((row["title"], row["description"], row["status"]),
                         ("Checkout fix", "Prices are off by a cent.", "open"))
        conn.close()

    def test_unknown_fields_are_refused(self):
        conn = db.connect()
        with self.assertRaises(ValueError):
            db.set_task(conn, "A-1", owner="me")
        conn.close()


class NamespaceTest(IsolatedTestCase):
    def test_off_by_default(self):
        self.assertEqual(config.Config().qualify("acme", "SHOP-1"), "SHOP-1")

    def test_bare_ids_get_the_client(self):
        cfg = config.Config({"tasks": {"namespace": True}})
        self.assertEqual(cfg.qualify("acme", "SHOP-1"), "acme:SHOP-1")
        self.assertEqual(cfg.qualify("globex", "48302"), "globex:48302")

    def test_full_keys_and_missing_clients_stay_as_they_are(self):
        cfg = config.Config({"tasks": {"namespace": True}})
        self.assertEqual(cfg.qualify("acme", "globex:48302"), "globex:48302")
        self.assertEqual(cfg.qualify(None, "SHOP-1"), "SHOP-1")
        self.assertIsNone(cfg.qualify("acme", None))


class NamespacedTrackingTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(CONFIG)
        self.conn = db.connect()
        self.t0 = local_ts("2026-09-15 10:00")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def busy(self, sid, branch="main"):
        for off, kind in ((0, "prompt"), (600, "stop")):
            db.record_event(self.conn, ts=self.t0 + off, session_id=sid, kind=kind, cwd=ACME,
                            project=ACME, branch=branch)
            db.touch_session(self.conn, sid, self.t0 + off, project_dir=ACME)

    def run_cli(self, *argv, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            if stdin is not None:
                import sys
                saved, sys.stdin = sys.stdin, io.StringIO(stdin)
            try:
                code = cli.main(list(argv), now=self.t0 + 7200, out=out)
            finally:
                if stdin is not None:
                    sys.stdin = saved
        return code, out.getvalue(), err.getvalue()

    def test_branch_task_carries_the_client(self):
        self.busy("s1abcdef", branch="feature/12345/x")
        cur = ledger.current(self.conn, config.load(), "s1abcdef", self.t0 + 600)
        self.assertEqual(cur.task, "acme:12345")

    def test_task_set_qualifies_a_bare_id(self):
        self.busy("s1abcdef")
        self.run_cli("task", "set", "SHOP-42", "--session", "s1abcdef")
        task = self.conn.execute("SELECT task FROM assignments").fetchone()["task"]
        self.assertEqual(task, "acme:SHOP-42")

    def test_prompt_candidates_are_offered_with_the_client(self):
        self.busy("s1abcdef")
        text = hook._prompt_context(self.conn, config.load(), "s1abcdef", "to jest SHOP-42", self.t0 + 600)
        self.assertIn("task set acme:SHOP-42", text)

    def test_tasks_set_and_show(self):
        plan = os.path.join(self.tmp, "plan.md")
        with open(plan, "w", encoding="utf-8") as f:
            f.write("1. Find the rounding.\n2. Round once.\n")
        code, _, _ = self.run_cli("tasks", "set", "acme:SHOP-42", "--title", "Checkout", "--plan", f"@{plan}",
                                  "--status", "open")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("tasks", "show", "acme:SHOP-42")
        self.assertEqual(code, 0)
        self.assertIn("status   open", out)
        self.assertIn("plan\n1. Find the rounding.\n2. Round once.", out)

    def test_tasks_set_needs_a_field(self):
        code, _, err = self.run_cli("tasks", "set", "acme:SHOP-42")
        self.assertEqual(code, 2)
        self.assertIn("nothing to set", err)

    def test_tasks_import_and_list(self):
        lines = "\n".join(json.dumps(x) for x in (
            {"task": "acme:SHOP-1", "title": "One", "status": "done", "plan": "p"},
            {"task": "globex:48302", "title": "Two", "status": "open"}))
        code, out, _ = self.run_cli("tasks", "import", "-", stdin=lines + "\n\n")
        self.assertEqual((code, out.strip()), (0, "imported 2 tasks"))
        _, out, _ = self.run_cli("tasks", "list", "--client", "acme")
        self.assertEqual(out.split(), ["acme:SHOP-1", "done", "One"])
        self.assertEqual(db.task_details(self.conn, "acme:SHOP-1")["plan"], "p")

    def test_a_bad_import_line_imports_nothing(self):
        lines = json.dumps({"task": "acme:SHOP-1", "title": "One"}) + "\n" + json.dumps({"title": "no key"})
        code, _, err = self.run_cli("tasks", "import", "-", stdin=lines)
        self.assertEqual(code, 2)
        self.assertIn("line 2", err)
        self.assertEqual(db.task_details(self.conn), {})


class DashboardDetailsTest(IsolatedTestCase):
    def test_task_rows_carry_description_plan_status_and_url(self):
        self.write_config(CONFIG)
        conn = db.connect()
        t0 = local_ts("2026-09-15 10:00")
        for off, kind in ((0, "prompt"), (600, "stop")):
            db.record_event(conn, ts=t0 + off, session_id="s1", kind=kind, cwd=ACME, project=ACME,
                            branch="feature/12345/x")
            db.touch_session(conn, "s1", t0 + off, project_dir=ACME)
        db.set_task(conn, "acme:12345", title="Pricing", description="d", plan="p", status="open",
                    url="https://redmine.example.com/issues/12345")
        data = dashboard.build_report(conn, config.load(), t0, t0 + 3600)
        conn.close()
        task = data["clients"][0]["tasks"][0]
        self.assertEqual({k: task[k] for k in ("task", "title", "description", "plan", "status", "url")},
                         {"task": "acme:12345", "title": "Pricing", "description": "d", "plan": "p",
                          "status": "open", "url": "https://redmine.example.com/issues/12345"})


if __name__ == "__main__":
    unittest.main()
