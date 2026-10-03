"""The work list: projects, numbered tasks, history, notes, metrics, ticket aliases."""

import contextlib
import io
import json
import os
import sqlite3
import subprocess

from tracker import cli, config, db, hook, ledger, status, worklist
from tests.helpers import IsolatedTestCase, local_ts


class RemoteTest(IsolatedTestCase):
    def test_ssh_and_https_remotes_are_the_same_project(self):
        for url in ("git@lab.example.com:shop/storefront.git", "https://lab.example.com/shop/storefront.git",
                    "ssh://git@lab.example.com/shop/storefront", "https://user@lab.example.com/shop/storefront"):
            self.assertEqual(worklist.normalize_remote(url), "lab.example.com/shop/storefront", url)
        self.assertEqual(worklist.normalize_remote(""), "")


class WorklistTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = os.path.realpath(self.tmp)
        self.repo = os.path.join(self.tmp, "clients", "acme", "storefront")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "remote", "add", "origin", "git@lab.example.com:acme/storefront.git"],
                       check=True)
        self.write_config(f'[[rule]]\npath = "{self.tmp}/clients/{{client}}/**"\n\n[tasks]\nnamespace = true\n')
        self.conn = db.connect()
        self.cfg = config.load()
        self.t0 = local_ts("2026-09-15 10:00")
        self._cwd = os.getcwd()
        os.chdir(self.repo)
        self._sid = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def tearDown(self):
        os.chdir(self._cwd)
        if self._sid is not None:
            os.environ["CLAUDE_CODE_SESSION_ID"] = self._sid
        self.conn.close()
        super().tearDown()

    def run_cli(self, *argv, stdin=None, now=None):
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            if stdin is not None:
                import sys
                saved, sys.stdin = sys.stdin, io.StringIO(stdin)
            try:
                code = cli.main(list(argv), now=now or self.t0 + 7200, out=out)
            finally:
                if stdin is not None:
                    sys.stdin = saved
        return code, out.getvalue(), err.getvalue()

    def busy(self, sid, branch="main", start=None, length=600):
        start = self.t0 if start is None else start
        for off, kind in ((0, "prompt"), (length, "stop")):
            db.record_event(self.conn, ts=start + off, session_id=sid, kind=kind, cwd=self.repo,
                            project=self.repo, branch=branch)
            db.touch_session(self.conn, sid, start + off, project_dir=self.repo)

    def add(self, title, *extra):
        code, out, err = self.run_cli("tasks", "add", title, *extra)
        self.assertEqual(code, 0, err)
        return out.strip()

    # ── projects and numbers ────────────────────────────────────────────────

    def test_first_add_creates_the_project_from_the_remote_with_the_client_from_the_rules(self):
        self.assertEqual(self.add("Checkout rounds twice"), "acme:storefront#1")
        self.assertEqual(self.add("Basket badge"), "acme:storefront#2")
        p = worklist.project(self.conn, "storefront")
        self.assertEqual((p["client"], p["remote"]), ("acme", "lab.example.com/acme/storefront"))

    def test_a_worktree_belongs_to_the_project_of_its_clone(self):
        self.add("One")
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "--allow-empty", "-m", "x"], check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        wt = os.path.join(self.tmp, "wt")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", wt], check=True)
        subprocess.run(["git", "-C", self.repo, "remote", "remove", "origin"], check=True)
        self.assertEqual(worklist.find_project(self.conn, wt)["slug"], "storefront")

    def test_a_task_is_found_by_number_project_number_key_or_ticket(self):
        self.add("One")
        self.add("Two", "--ticket", "SHOP-42")
        proj = worklist.project(self.conn, "storefront")
        for ref in ("2", "#2", "storefront#2", "acme:storefront#2", "SHOP-42", "shop-42"):
            self.assertEqual(worklist.resolve(self.conn, ref, proj), "acme:storefront#2", ref)
        self.assertIsNone(worklist.resolve(self.conn, "#9", proj))

    def test_numbers_are_unique_per_project(self):
        self.add("One")
        with self.assertRaises(sqlite3.IntegrityError):
            db.set_task(self.conn, "acme:other", project="storefront", number=1)

    # ── changes ─────────────────────────────────────────────────────────────

    def test_set_records_history_and_appends_to_lists(self):
        key = self.add("One", "--pitfall", "prices are cached")
        self.run_cli("tasks", "set", "1", "--next-step", "round once", "--pitfall", "tax is per line",
                     "--pitfall", "prices are cached", "--author", "claude")
        row = db.task_details(self.conn, key)
        self.assertEqual(json.loads(row["pitfalls"]), ["prices are cached", "tax is per line"])
        hist = [(h["field"], h["after"], h["author"]) for h in worklist.history(self.conn, key)]
        self.assertIn(("next_step", "round once", "claude"), hist)
        self.assertEqual(hist[0][0], "created")
        code, out, _ = self.run_cli("tasks", "set", "1", "--next-step", "round once")
        self.assertIn("no change", out)

    def test_a_plain_key_still_works_as_before(self):
        code, out, _ = self.run_cli("tasks", "set", "acme:SHOP-7", "--title", "Plain", "--status", "open")
        self.assertEqual(code, 0)
        self.assertEqual(db.task_details(self.conn, "acme:SHOP-7")["title"], "Plain")

    def test_done_needs_an_outcome_and_keeps_metrics(self):
        key = self.add("One")
        code, _, err = self.run_cli("tasks", "done", "1")
        self.assertEqual(code, 2)
        self.assertIn("outcome", err)
        code, _, err = self.run_cli("tasks", "done", "1", "--outcome", "rounded once",
                                    "--metric", "cent errors", "14", "0", "orders from 09-01")
        self.assertEqual(code, 0, err)
        row = db.task_details(self.conn, key)
        self.assertEqual((row["status"], row["outcome"], row["closed_at"]), ("done", "rounded once", self.t0 + 7200))
        m = worklist.metrics(self.conn, key)[0]
        self.assertEqual((m["name"], m["before"], m["after"], m["method"]), ("cent errors", "14", "0",
                                                                             "orders from 09-01"))

    def test_a_metric_needs_its_method(self):
        self.add("One")
        code, _, err = self.run_cli("tasks", "metric", "1", "x", "1", "2")
        self.assertEqual(code, 2)

    def test_park_and_reopen(self):
        self.add("One")
        self.run_cli("tasks", "done", "1", "--park", "--outcome", "waiting for the client")
        code, _, err = self.run_cli("tasks", "start", "1")
        self.assertEqual(code, 2)
        self.assertIn("--reopen", err)
        code, _, _ = self.run_cli("tasks", "start", "1", "--reopen")
        self.assertEqual(code, 0)
        self.assertEqual(db.task_details(self.conn, "acme:storefront#1")["status"], "in-progress")

    # ── time ────────────────────────────────────────────────────────────────

    def test_start_logs_the_session_from_its_beginning_and_time_shows_up(self):
        key = self.add("One")
        self.busy("s1abcdef")
        code, out, err = self.run_cli("tasks", "start", "1", "--session", "s1abcdef")
        self.assertEqual(code, 0, err)
        self.assertEqual(db.task_details(self.conn, key)["status"], "in-progress")
        self.assertEqual(worklist.seconds_per_task(self.conn, self.cfg, end=self.t0 + 3600), {key: 600.0})
        _, out, _ = self.run_cli("tasks", "list", "--open", "--project", ".", "--time")
        self.assertIn("0:10", out)

    def test_start_takes_the_session_from_the_environment(self):
        self.add("One")
        self.busy("s1abcdef")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1abcdef"
        try:
            self.run_cli("tasks", "start", "1")
        finally:
            del os.environ["CLAUDE_CODE_SESSION_ID"]
        self.assertEqual(self.conn.execute("SELECT task FROM assignments").fetchone()["task"], "acme:storefront#1")

    def test_a_branch_named_after_the_ticket_logs_time_to_the_task(self):
        key = self.add("Checkout", "--ticket", "SHOP-42")
        self.busy("s1abcdef", branch="feature/SHOP-42-rounding")
        alloc = ledger.allocate(self.conn, self.cfg, self.t0, self.t0 + 3600)
        self.assertEqual({k.task for k, _ in alloc}, {key})

    def test_old_assignments_to_the_ticket_count_for_the_task(self):
        self.busy("s1abcdef")
        db.add_assignment(self.conn, "s1abcdef", "acme:SHOP-42", self.t0, "cli")
        key = self.add("Checkout", "--ticket", "SHOP-42")
        self.assertEqual(worklist.seconds_per_task(self.conn, self.cfg, end=self.t0 + 3600), {key: 600.0})

    def test_a_ticket_on_a_decision_does_not_steal_it_from_the_task(self):
        task = self.add("Checkout", "--ticket", "SHOP-42")
        self.add("Round in the basket, not in the PDF", "--ticket", "SHOP-42", "--kind", "decision")
        self.assertEqual(db.task_aliases(self.conn)["acme:SHOP-42"], task)

    # ── what Claude is told ─────────────────────────────────────────────────

    def test_show_is_a_resume_package(self):
        self.add("Checkout", "--ticket", "SHOP-42", "--criteria", "14 cent errors -> 0, orders since 09-01")
        self.run_cli("tasks", "set", "1", "--next-step", "round in Basket::total", "--pitfall", "cache")
        self.run_cli("tasks", "note", "1", "price comes from two places")
        self.run_cli("tasks", "metric", "1", "errors", "14", "3", "--method", "orders since 09-01")
        self.run_cli("tasks", "project", "storefront", "--ticket-url", "https://jira.example.com/browse/{ticket}")
        code, out, _ = self.run_cli("tasks", "show", "SHOP-42")
        self.assertEqual(code, 0)
        for part in ("next step\nround in Basket::total", "criteria\n14 cent errors", "pitfalls\n  - cache",
                     "errors: 14 → 3  (orders since 09-01)", "price comes from two places",
                     "url      https://jira.example.com/browse/SHOP-42"):
            self.assertIn(part, out)

    def test_session_start_tells_the_next_step_of_the_current_task(self):
        key = self.add("Checkout")
        self.run_cli("tasks", "set", "1", "--next-step", "round in Basket::total")
        self.busy("s1abcdef")
        db.add_assignment(self.conn, "s1abcdef", key, self.t0, "cli")
        out = hook.handle({"session_id": "s1abcdef", "hook_event_name": "SessionStart", "cwd": self.repo},
                          self.conn, self.cfg, self.t0 + 700, {})
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Next step: round in Basket::total", text)
        self.assertIn("tasks list --open --project .", text)

    def test_no_work_list_no_hint(self):
        self.busy("s1abcdef")
        out = hook.handle({"session_id": "s1abcdef", "hook_event_name": "SessionStart", "cwd": self.repo},
                          self.conn, self.cfg, self.t0 + 700, {})
        self.assertNotIn("Work list", out["hookSpecificOutput"]["additionalContext"])

    def test_a_prompt_naming_the_ticket_of_the_current_task_asks_nothing(self):
        key = self.add("Checkout", "--ticket", "SHOP-42")
        self.busy("s1abcdef")
        db.add_assignment(self.conn, "s1abcdef", key, self.t0, "cli")
        self.assertIsNone(hook._prompt_context(self.conn, self.cfg, "s1abcdef", "about SHOP-42", self.t0 + 700))

    def test_the_status_file_lists_the_open_tasks_most_pressing_first(self):
        self.add("Plain", "--priority", "low")                              # 1
        self.add("Medium", "--priority", "medium")                          # 2
        self.add("Risky", "--priority", "risk")                             # 3
        started = self.add("Started")                                       # 4
        self.run_cli("tasks", "start", started, "--session", "s1abcdef")
        self.add("Rule", "--kind", "decision")                              # 5
        finished = self.add("Finished")                                     # 6
        self.run_cli("tasks", "done", finished, "--outcome", "shipped")
        self.busy("s1abcdef")
        got = status.write(self.conn, self.cfg, "s1abcdef", self.t0 + 700)["open"]
        self.assertEqual((got["project"], got["count"]), ("storefront", 4))
        self.assertEqual([i["number"] for i in got["items"]], [3, 4, 2, 1])
        self.assertEqual(got["items"][1]["status"], "in-progress")

    def test_outside_any_project_there_is_no_list(self):
        self.assertIsNone(status.open_tasks(self.conn, self.tmp))
        self.assertIsNone(status.open_tasks(self.conn, None))

    # ── storage ─────────────────────────────────────────────────────────────

    def test_a_version_3_database_gains_the_work_list(self):
        path = os.path.join(self.tmp, "v3.db")
        raw = sqlite3.connect(path)
        raw.executescript("CREATE TABLE tasks (task TEXT PRIMARY KEY, title TEXT, updated_at REAL, description TEXT,"
                          " plan TEXT, status TEXT, url TEXT); INSERT INTO tasks (task, title, plan) VALUES"
                          " ('A-1', 'x', 'p'); PRAGMA user_version = 3;")
        raw.close()
        conn = db.connect(path)
        self.assertEqual(db.task_details(conn, "A-1")["plan"], "p")
        for table in ("projects", "task_notes", "task_metrics", "task_history", "v_tasks"):
            conn.execute(f"SELECT * FROM {table}").fetchall()
        conn.close()

    def test_hist_lists_changes(self):
        self.add("One")
        self.run_cli("tasks", "set", "1", "--status", "waiting")
        code, out, _ = self.run_cli("tasks", "hist", "1")
        self.assertIn("status: open → waiting", out)
