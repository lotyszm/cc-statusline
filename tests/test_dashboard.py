import contextlib
import io
import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from tracker import dashboard, db, paths, worklist
from tests.helpers import IsolatedTestCase, local_ts

HOME = os.path.expanduser("~")
ACME = f"{HOME}/dev/agency/acme/storefront"
Globex = f"{HOME}/dev/agency/Globex/globex-shop"

CONFIG = """
currency = "PLN"
[[rule]]
path = "~/dev/agency/{client}/**"
[clients.acme]
rate = 150
"""


class ServedTestCase(IsolatedTestCase):
    """A dashboard on a free port over two sessions, one on branch feature/12345/x."""

    def setUp(self):
        super().setUp()
        self.write_config(CONFIG)
        conn = db.connect()
        t0 = local_ts("2026-09-15 10:00")
        for sid, cwd, branch, start, end in (("s1aaaaaa", ACME, "feature/12345/x", 0, 3600),
                                             ("s2bbbbbb", Globex, "main", 1800, 2400)):
            steps = [(off, "agent") for off in range(start + 600, end, 600)]
            for off, kind in [(start, "prompt")] + steps + [(end, "stop")]:
                db.record_event(conn, ts=t0 + off, session_id=sid, kind=kind, cwd=cwd, project=cwd,
                                branch=branch)
                db.touch_session(conn, sid, t0 + off, project_dir=cwd)
        db.set_task_title(conn, "12345", "Hotfix pricing")
        conn.close()
        self.server = dashboard.make_server(0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def report(self):
        status, _, raw = self.request("/api/report?from=2026-09-15&to=2026-09-15")
        self.assertEqual(status, 200)
        return json.loads(raw)

class DashboardTest(ServedTestCase):
    def test_report_splits_parallel_time_and_prices_it(self):
        data = self.report()
        clients = {c["client"]: c for c in data["clients"]}
        # 10:30-10:40 both sessions run: acme gets 3600 - 300, Globex 600 - 300.
        self.assertEqual(round(clients["acme"]["seconds"]), 3300)
        self.assertEqual(round(clients["Globex"]["seconds"]), 300)
        self.assertEqual(round(data["totals"]["clock_seconds"]), 3600)
        task = clients["acme"]["tasks"][0]
        self.assertEqual((task["task"], task["title"], round(task["amount"], 2)),
                         ("12345", "Hotfix pricing", 137.5))

    def test_unassigned_billable_time_is_listed_per_session(self):
        sessions = {s["short"]: s for s in self.report()["sessions"]}
        self.assertEqual(round(sessions["s2bbbbbb"]["unassigned_seconds"]), 300)
        self.assertEqual(sessions["s1aaaaaa"]["unassigned_seconds"], 0)

    def test_days_are_broken_down_by_client_and_task_state(self):
        days = self.report()["days"]
        self.assertEqual([d["day"] for d in days], ["2026-09-15"])
        rows = {(r["client"], r["billable"], r["assigned"]): round(r["seconds"]) for r in days[0]["rows"]}
        self.assertEqual(rows, {("acme", True, True): 3300, ("Globex", True, False): 300})

    def test_numbers_add_up_across_every_view(self):
        d = self.report()
        total = d["totals"]["seconds"]
        self.assertAlmostEqual(sum(c["seconds"] for c in d["clients"]), total, places=6)
        self.assertAlmostEqual(sum(t["seconds"] for c in d["clients"] for t in c["tasks"]), total, places=6)
        self.assertAlmostEqual(sum(r["seconds"] for day in d["days"] for r in day["rows"]), total, places=6)
        self.assertAlmostEqual(sum(s["seconds"] for s in d["sessions"]), total, places=6)
        untasked = sum(t["seconds"] for c in d["clients"] if c["billable"] for t in c["tasks"] if t["task"] is None)
        self.assertAlmostEqual(d["totals"]["unassigned_seconds"], untasked, places=6)

    def test_task_rows_carry_days_and_what_was_done(self):
        conn = db.connect()
        db.set_session_title(conn, "s1aaaaaa", "Fix the sale price")
        conn.close()
        task = {c["client"]: c for c in self.report()["clients"]}["acme"]["tasks"][0]
        self.assertEqual({k: round(v) for k, v in task["days"].items()}, {"2026-09-15": 3300})
        self.assertEqual((task["first_day"], task["last_day"]), ("2026-09-15", "2026-09-15"))
        self.assertEqual(task["titles"], ["Fix the sale price"])

    def test_session_endpoint_explains_how_time_was_counted(self):
        status, _, raw = self.request("/api/session?id=s2bb")
        self.assertEqual(status, 200)
        b = json.loads(raw)
        self.assertEqual([(x["kind"], round(x["seconds"]), round(x["counted"])) for x in b["blocks"]],
                         [("work", 600, 300)])

    def test_assign_from_the_dashboard(self):
        status, _, _ = self.request("/api/assign", {"session_id": "s2bbbbbb", "task": "SHOP-42",
                                                    "title": "Checkout", "from_start": True},
                                    {"Content-Type": "application/json", "X-CC-Statusline": "1"})
        self.assertEqual(status, 200)
        sessions = {s["short"]: s for s in self.report()["sessions"]}
        self.assertEqual((sessions["s2bbbbbb"]["tasks"], sessions["s2bbbbbb"]["unassigned_seconds"]),
                         (["SHOP-42"], 0))

    def test_assign_without_the_header_is_refused(self):
        status, _, _ = self.request("/api/assign", {"session_id": "s2bbbbbb", "task": "X-1"},
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_foreign_host_header_is_refused(self):
        status, _, _ = self.request("/api/report", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)

    def test_report_names_the_config_file_in_use(self):
        status, _, raw = self.request("/api/report?from=2026-09-01&to=2026-09-30")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["settings"]["config_path"], str(paths.config_path()))

    def test_page_is_served(self):
        status, ctype, body = self.request("/")
        self.assertEqual((status, ctype.split(";")[0]), (200, "text/html"))
        self.assertIn(b"<html", body.lower())

    def test_the_time_page_opens_on_today_with_arrows_to_step_the_range(self):
        _, _, raw = self.request("/")
        page = raw.decode()
        self.assertIn('const DEFAULT_PRESET = "today";', page)
        for arrow in ('id="range-prev"', 'id="range-next"'):
            self.assertIn(arrow, page)

    def test_both_pages_carry_the_theme_switch(self):
        for path in ("/", "/tasks"):
            _, _, raw = self.request(path)
            page = raw.decode()
            # Read in <head>, so a dark page never flashes light on the way in.
            self.assertIn("cc-statusline-theme", page.split("</head>")[0], path)
            for choice in ("auto", "light", "dark"):
                self.assertIn(f'data-theme-choice="{choice}"', page, path)



class ServeTest(IsolatedTestCase):
    """What `cc-statusline dashboard` does when the port is already taken."""

    def setUp(self):
        super().setUp()
        self.out = io.StringIO()

    def occupy_with_a_dashboard(self):
        server = dashboard.make_server(0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def occupy_with_something_else(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        return sock.getsockname()[1]

    def test_answers_tells_our_dashboard_from_other_listeners(self):
        self.assertTrue(dashboard.answers(self.occupy_with_a_dashboard()))
        self.assertFalse(dashboard.answers(self.occupy_with_something_else(), timeout=0.3))

    def test_a_running_dashboard_is_reused(self):
        port = self.occupy_with_a_dashboard()
        self.assertEqual(dashboard.serve(port, open_browser=False, out=self.out), 0)
        self.assertIn(f"already running: http://127.0.0.1:{port}/", self.out.getvalue())

    def test_the_service_steps_aside_quietly_when_the_port_is_taken(self):
        # Exit status 0 keeps launchd and systemd from restarting it in a loop.
        port = self.occupy_with_something_else()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dashboard.serve(port, open_browser=False, out=self.out, service=True), 0)

    def test_by_hand_a_taken_port_is_an_error(self):
        port = self.occupy_with_something_else()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(dashboard.serve(port, open_browser=False, out=self.out), 1)
        self.assertIn(f"cannot listen on 127.0.0.1:{port}", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class WorkListViewTest(ServedTestCase):
    def setUp(self):
        super().setUp()
        conn = db.connect()
        conn.execute("INSERT INTO projects (slug, client, path, ticket_url, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("storefront", "acme", ACME, "https://jira.example/browse/{ticket}", 0))
        proj = worklist.project(conn, "storefront")
        self.key = worklist.add(conn, proj, "Hotfix pricing", ticket="12345", next_step="deploy",
                                description="Prices round the wrong way", pitfalls='["cache"]')
        worklist.add(conn, proj, "Old checkout", status="done")
        worklist.add(conn, proj, "Keep VAT in the API", kind="decision", status="in-force")
        worklist.note(conn, self.key, "found the rounding")
        worklist.metric(conn, self.key, "wrong prices", "12", "0", "SQL count")
        conn.close()

    def tasks(self, query=""):
        status, ctype, raw = self.request(f"/api/tasks{query}")
        self.assertEqual((status, ctype), (200, "application/json; charset=utf-8"))
        return json.loads(raw)

    def test_the_page_is_served(self):
        status, ctype, raw = self.request("/tasks")
        self.assertEqual((status, ctype), (200, "text/html; charset=utf-8"))
        self.assertIn(b"/api/tasks", raw)

    def test_the_page_carries_a_guide_to_the_work_list(self):
        _, _, raw = self.request("/tasks")
        page = raw.decode()
        self.assertIn('<template id="guide">', page)
        self.assertIn('id="guide-open"', page)          # the header button that brings it back
        for command in ("tasks add", "tasks start", "--next-step", "tasks done", "--outcome", "--ticket"):
            self.assertIn(command, page)

    def test_open_view_lists_open_tasks_with_time_and_ticket_link(self):
        data = self.tasks()
        self.assertEqual([t["title"] for t in data["tasks"]], ["Hotfix pricing"])
        t = data["tasks"][0]
        # The branch feature/12345/x counts for the task that carries ticket 12345.
        self.assertEqual((t["task"], t["client"], t["ticket_url"], t["next_step"]),
                         (self.key, "acme", "https://jira.example/browse/12345", "deploy"))
        self.assertGreater(t["seconds"], 3000)
        self.assertEqual(data["projects"][0], {"slug": "storefront", "client": "acme", "open": 1})

    def test_views_and_search_filter(self):
        self.assertEqual([t["title"] for t in self.tasks("?view=done")["tasks"]], ["Old checkout"])
        self.assertEqual([t["title"] for t in self.tasks("?view=decisions")["tasks"]], ["Keep VAT in the API"])
        self.assertEqual(len(self.tasks("?view=all")["tasks"]), 3)
        self.assertEqual([t["title"] for t in self.tasks("?view=all&q=vat")["tasks"]], ["Keep VAT in the API"])
        self.assertEqual(self.tasks("?project=nowhere")["tasks"], [])

    def test_one_task_in_full(self):
        status, _, raw = self.request(f"/api/task?key={urllib.parse.quote(self.key)}")
        self.assertEqual(status, 200)
        t = json.loads(raw)
        self.assertEqual((t["description"], t["pitfalls"]), ("Prices round the wrong way", ["cache"]))
        self.assertEqual([n["text"] for n in t["notes"]], ["found the rounding"])
        self.assertEqual([(m["before"], m["after"], m["method"]) for m in t["metrics"]], [("12", "0", "SQL count")])
        self.assertEqual(t["history"][0]["field"], "created")
        self.assertEqual([s["short"] for s in t["sessions"]], ["s1aaaaaa"])

    def test_unknown_task_is_404(self):
        status, _, _ = self.request("/api/task?key=nope%231")
        self.assertEqual(status, 404)

    def test_task_reads_check_the_host(self):
        status, _, _ = self.request("/api/tasks", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)

    def post(self, path, body, header=True):
        headers = {"Content-Type": "application/json", **({"X-CC-Statusline": "1"} if header else {})}
        status, _, raw = self.request(path, body, headers)
        return status, json.loads(raw)

    def test_writes_need_the_header(self):
        status, _ = self.post("/api/task/status", {"key": self.key, "status": "waiting"}, header=False)
        self.assertEqual(status, 403)
        self.assertEqual(db.task_details(db.connect(), self.key)["status"], "open")

    def test_status_change(self):
        status, body = self.post("/api/task/status", {"key": self.key, "status": "done"})
        self.assertEqual((status, body["error"]), (400, "closing needs an outcome: what was actually done"))
        status, body = self.post("/api/task/status", {"key": self.key, "status": "done", "outcome": "rounded"})
        self.assertEqual((status, body["task"]["status"], body["task"]["outcome"]), (200, "done", "rounded"))
        self.assertEqual(body["task"]["history"][-1]["author"], "dashboard")
        status, _ = self.post("/api/task/status", {"key": self.key, "status": "sideways"})
        self.assertEqual(status, 400)

    def test_move_to_another_project(self):
        conn = db.connect()
        conn.execute("INSERT INTO projects (slug, client, path, created_at) VALUES ('globex-shop', 'Globex', ?, 0)",
                     (Globex,))
        conn.close()
        status, body = self.post("/api/task/move", {"key": self.key, "project": "globex-shop"})
        self.assertEqual(status, 200)
        t = body["task"]
        self.assertEqual((t["task"], t["client"], t["moved_from"]), ("Globex:globex-shop#1", "Globex", [self.key]))
        self.assertEqual([n["text"] for n in t["notes"]], ["found the rounding"])
        status, _ = self.post("/api/task/move", {"key": self.key, "project": "globex-shop"})
        self.assertEqual(status, 404)                     # the old key is gone from the list...
        status, _, raw = self.request(f"/api/tasks?view=all")
        self.assertIn("Globex:globex-shop#1", [x["task"] for x in json.loads(raw)["tasks"]])
