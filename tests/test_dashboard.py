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


# The JSON contract of the FlowApp page (context: the plan both sides build to).
FLOWAPP_STATUS_KEYS = {"enabled", "problem", "message", "name", "expires_at", "scopes", "round_minutes",
                       "gap_minutes"}
FLOWAPP_PREVIEW_CONTRACT = {
    "from": "2026-11-10", "to": "2026-11-16", "today": "2026-11-16", "remote_ok": True,
    "days": [{"day": "2026-11-10", "log_seconds": 0, "spent_seconds": 0, "tracked_seconds": 0, "rounded_seconds": 0,
              "tasks": [{"key": "d24:cms#120", "client": "d24", "title": "Toolbar icons", "tracked_seconds": 0,
                         "rounded_seconds": 0,
                         "blocks": [{"external_id": "cc:d24:cms#120:1762762320", "day": "2026-11-10",
                                     "start": "", "stop": "", "tracked_start": "", "tracked_stop": "",
                                     "tracked_seconds": 0, "active_seconds": 0, "comment": "", "redmine_id": "31", "status": "new",
                                     "flags": [], "error": None, "flowapp_id": None, "export_status": None}]}]}]}
FLOWAPP_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "flowapp_preview.json")
FLOWAPP_TOKEN = "flw_dashboard_test_token_123"


def key_shape(value):
    """The keys of a JSON value, recursively: lists by the union of their items' shapes."""
    if isinstance(value, dict):
        return {k: key_shape(v) for k, v in value.items()}
    if isinstance(value, list):
        shapes = [key_shape(v) for v in value if isinstance(v, (dict, list))]
        merged = {}
        for s in shapes:
            if isinstance(s, dict):
                merged.update(s)
        return [merged] if merged else []
    return None


class FlowappEndpointTest(ServedTestCase):
    """The FlowApp page's API against a fake FlowApp: no real request ever leaves."""

    def setUp(self):
        super().setUp()
        from tests.test_flowapp_client import FakeFlowApp
        self.fake = FakeFlowApp()
        self.write_config(CONFIG + f'\n[flowapp]\nurl = "{self.fake.url}"\n')
        dashboard.flowapp_reset()
        self._load_token = dashboard.load_token
        dashboard.load_token = lambda: (FLOWAPP_TOKEN, None)
        # The fake answers to its own token; the dashboard is given this one.
        import tests.test_flowapp_client as fake_mod
        self._fake_token = fake_mod.TOKEN
        fake_mod.TOKEN = FLOWAPP_TOKEN
        # Yesterday 10:00-10:34 on task 12345 (branch feature/12345/x), billable acme.
        from datetime import date, timedelta
        self.day = (date.today() - timedelta(days=1)).isoformat()
        conn = db.connect()
        t0 = local_ts(f"{self.day} 10:00")
        for off, kind in ((0, "prompt"), (600, "agent"), (1200, "agent"), (2040, "stop")):
            db.record_event(conn, ts=t0 + off, session_id="s3cccccc", kind=kind, cwd=ACME, project=ACME,
                            branch="feature/12345/x")
            db.touch_session(conn, "s3cccccc", t0 + off, project_dir=ACME)
        conn.close()
        self.ext = f"cc:12345:{int(t0)}"

    def tearDown(self):
        import tests.test_flowapp_client as fake_mod
        fake_mod.TOKEN = self._fake_token
        dashboard.load_token = self._load_token
        job = dashboard._current_job
        if job is not None:
            job.cancel()
            job.join(5)
        dashboard.flowapp_reset()
        self.fake.close()
        super().tearDown()

    def get(self, path):
        status, _, raw = self.request(path)
        self.assertNotIn(FLOWAPP_TOKEN.encode(), raw, path)
        return status, json.loads(raw)

    def post(self, path, body, header=True):
        headers = {"Content-Type": "application/json", **({"X-CC-Statusline": "1"} if header else {})}
        status, _, raw = self.request(path, body, headers)
        self.assertNotIn(FLOWAPP_TOKEN.encode(), raw, path)
        return status, json.loads(raw)

    def preview(self):
        status, body = self.get(f"/api/flowapp/preview?from={self.day}&to={self.day}&billable=1")
        self.assertEqual(status, 200)
        return body

    def wait_for(self, job_id):
        import time
        for _ in range(200):
            status, snap = self.get(f"/api/flowapp/job?id={job_id}")
            self.assertEqual(status, 200)
            if not snap["running"]:
                return snap
            time.sleep(0.05)
        self.fail("the send job did not finish")

    def test_status_names_the_account_and_never_the_token(self):
        status, body = self.get("/api/flowapp/status")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), FLOWAPP_STATUS_KEYS)
        self.assertEqual((body["enabled"], body["problem"], body["name"], body["expires_at"], body["scopes"],
                          body["round_minutes"], body["gap_minutes"]),
                         (True, None, "Test User", "2027-02-14T09:01:53.417Z", ["tracker:read", "tracker:write"], 5, 15))

    def test_me_is_cached(self):
        self.get("/api/flowapp/status")
        self.get("/api/flowapp/status")
        self.assertEqual(sum(r["path"].endswith("/me") for r in self.fake.requests), 1)

    def test_off_without_a_url(self):
        self.write_config(CONFIG)
        status, body = self.get("/api/flowapp/status")
        self.assertEqual((status, body["enabled"], body["problem"]), (200, False, "no_url"))
        self.assertEqual(self.fake.requests, [])
        status, body = self.get("/api/flowapp/preview")
        self.assertEqual((status, body["problem"]), (409, "no_url"))

    def test_token_problems_are_reported(self):
        for problem in ("no_token", "token_file_mode"):
            with self.subTest(problem=problem):
                dashboard.flowapp_reset()
                dashboard.load_token = lambda p=problem: (None, p)
                status, body = self.get("/api/flowapp/status")
                self.assertEqual((body["enabled"], body["problem"]), (False, problem))
                self.assertTrue(body["message"])

    def test_refused_token_is_unauthorized(self):
        import tests.test_flowapp_client as fake_mod
        fake_mod.TOKEN = "flw_another"
        _, body = self.get("/api/flowapp/status")
        self.assertEqual((body["enabled"], body["problem"], body["message"]), (False, "unauthorized", "Brak autoryzacji"))

    def test_a_token_flowapp_repeats_never_leaves_the_dashboard(self):
        echo = f"Token expired: Bearer {FLOWAPP_TOKEN}"
        self.fake.get_error = (401, {"error": {"code": "unauthorized", "message": echo}})
        _, body = self.get("/api/flowapp/status")         # get() and post() fail on the token in any answer
        self.assertEqual((body["problem"], body["message"]), ("unauthorized", "Token expired: Bearer [token]"))
        self.fake.get_error = None
        dashboard.flowapp_reset()
        self.fake.script = [(400, {"error": {"code": "invalid_request", "message": echo}}, {})]
        _, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        snap = self.wait_for(body["job_id"])
        self.assertEqual(snap["errors"][0]["message"], "Token expired: Bearer [token]")
        block = self.preview()["days"][0]["tasks"][0]["blocks"][0]
        self.assertEqual((block["status"], block["error"]), ("error", "invalid_request: Token expired: Bearer [token]"))
        self.fake.script = [(401, {"error": {"code": "unauthorized", "message": echo}}, {})]
        _, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        self.assertEqual(self.wait_for(body["job_id"])["fatal"]["message"], "Token expired: Bearer [token]")
        conn = db.connect()
        stored = [tuple(r) for r in conn.execute("SELECT * FROM flowapp_exports")]
        conn.close()
        self.assertNotIn(FLOWAPP_TOKEN, repr(stored))

    def test_every_flowapp_answer_has_the_token_cut_out(self):
        self.get("/api/flowapp/status")                 # the token is looked up once, as on any first visit
        leak = {"note": f"slipped in: {FLOWAPP_TOKEN}"}

        class Job:
            def snapshot(self):
                return leak

        def boom(*a, **k):
            raise ValueError(f"bad: {FLOWAPP_TOKEN}")

        names = ("flowapp_status", "flowapp_job", "flowapp_preview", "flowapp_draft", "flowapp_send")
        saved = {n: getattr(dashboard, n) for n in names}
        try:
            dashboard.flowapp_status = lambda cfg: leak
            dashboard.flowapp_job = lambda job_id: Job()
            dashboard.flowapp_preview = lambda *a, **k: leak
            dashboard.flowapp_draft = lambda *a, **k: leak
            dashboard.flowapp_send = lambda *a, **k: (200, leak)
            for path in ("/api/flowapp/status", "/api/flowapp/job?id=x", f"/api/flowapp/preview?from={self.day}"):
                with self.subTest(path=path):
                    self.assertEqual(self.get(path), (200, {"note": "slipped in: [token]"}))
            for path in ("/api/flowapp/draft", "/api/flowapp/send"):
                with self.subTest(path=path):
                    self.assertEqual(self.post(path, {"external_ids": ["x"]}), (200, {"note": "slipped in: [token]"}))
            dashboard.flowapp_preview = dashboard.flowapp_draft = boom
            self.assertEqual(self.get(f"/api/flowapp/preview?from={self.day}"), (400, {"error": "bad: [token]"}))
            self.assertEqual(self.post("/api/flowapp/draft", {}), (400, {"error": "bad: [token]"}))
        finally:
            for n, f in saved.items():
                setattr(dashboard, n, f)

    def test_unreachable_flowapp_degrades(self):
        self.fake.close()
        _, body = self.get("/api/flowapp/status")
        self.assertEqual((body["enabled"], body["problem"]), (True, "unreachable"))
        data = self.preview()
        self.assertFalse(data["remote_ok"])
        self.assertEqual(data["days"][0]["tasks"][0]["blocks"][0]["status"], "new")
        status, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        self.assertEqual(status, 503)
        from tests.test_flowapp_client import FakeFlowApp
        self.fake = FakeFlowApp()

    def test_preview_follows_the_contract(self):
        data = self.preview()
        self.assertEqual(key_shape(data), key_shape(FLOWAPP_PREVIEW_CONTRACT))
        day = data["days"][0]
        block = day["tasks"][0]["blocks"][0]
        self.assertEqual((day["day"], day["tasks"][0]["key"], day["tasks"][0]["title"], block["external_id"],
                          block["status"], block["comment"]),
                         (self.day, "12345", "Hotfix pricing", self.ext, "new", "Hotfix pricing"))
        self.assertEqual((day["tracked_seconds"], day["rounded_seconds"], day["log_seconds"], day["spent_seconds"]),
                         (34 * 60, 35 * 60, 35 * 60, 35 * 60))
        self.assertTrue(block["start"].startswith(f"{self.day}T10:00:00+0"))

    @unittest.skipUnless(os.path.exists(FLOWAPP_FIXTURE), "the page's fixture comes with the page")
    def test_preview_matches_the_page_fixture(self):
        with open(FLOWAPP_FIXTURE, encoding="utf-8") as f:
            fixture = json.load(f)
        self.assertEqual(key_shape(self.preview()), key_shape(fixture))

    def test_edit_survives_a_reload(self):
        status, body = self.post("/api/flowapp/draft", {"day": self.day, "external_id": self.ext, "action": "edit",
                                                        "comment": "Pricing hotfix", "redmine_id": "31",
                                                        "stop": "10:45"})
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "task"})
        self.assertEqual(set(body["task"]), {"key", "client", "title", "tracked_seconds", "rounded_seconds", "blocks"})
        block = self.preview()["days"][0]["tasks"][0]["blocks"][0]
        self.assertEqual((block["status"], block["comment"], block["redmine_id"], block["stop"][11:16]),
                         ("draft", "Pricing hotfix", "31", "10:45"))

    def test_invalid_edit_is_400(self):
        for change in ({"stop": "09:00"}, {"stop": "25:00"}, {"redmine_id": "CMS-1"}):
            with self.subTest(change=change):
                status, body = self.post("/api/flowapp/draft", {"day": self.day, "external_id": self.ext,
                                                                "action": "edit", **change})
                self.assertEqual(status, 400)
                self.assertTrue(body["error"])

    def test_unknown_entry_is_404(self):
        status, _ = self.post("/api/flowapp/draft", {"day": self.day, "external_id": "cc:nope:1", "action": "skip"})
        self.assertEqual(status, 404)

    def entries_asked(self):
        return sum(1 for r in self.fake.requests if r["method"] == "GET" and "/tracker/entries" in r["path"])

    def test_drafts_do_not_ask_flowapp(self):
        conn = db.connect()
        t1 = local_ts(f"{self.day} 13:00")
        for off, kind in ((0, "prompt"), (600, "stop")):     # a second block of the task that day
            db.record_event(conn, ts=t1 + off, session_id="s3cccccc", kind=kind, cwd=ACME, project=ACME,
                            branch="feature/12345/x")
            db.touch_session(conn, "s3cccccc", t1 + off, project_dir=ACME)
        conn.close()
        self.preview()
        asked = self.entries_asked()
        for body in ({"action": "edit", "comment": "Pricing fix"}, {"action": "skip"}, {"action": "unskip"},
                     {"action": "merge_next"}):
            with self.subTest(action=body["action"]):
                status, answer = self.post("/api/flowapp/draft", {"day": self.day, "external_id": self.ext, **body})
                self.assertEqual(status, 200, answer)
        self.assertEqual(self.entries_asked() - asked, 0)
        self.assertEqual([b["status"] for b in answer["task"]["blocks"]], ["draft"])

    def test_a_draft_keeps_what_the_preview_learnt_from_flowapp(self):
        _, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        self.wait_for(body["job_id"])
        self.fake.entries = [{**e, "exportStatus": "exported"} for e in self.fake.stored.values()]
        self.preview()
        asked = self.entries_asked()
        status, answer = self.post("/api/flowapp/draft", {"day": self.day, "external_id": self.ext, "action": "edit",
                                                          "comment": "Pricing fix"})
        block = answer["task"]["blocks"][0]
        self.assertEqual((status, block["status"], block["flags"]), (200, "changed", ["exported"]))
        self.assertEqual(self.entries_asked() - asked, 0)

    def test_a_stored_entry_of_unbilled_time_stays_out_of_the_billable_view(self):
        own = f"{HOME}/dev/own/me/blog"
        self.write_config(CONFIG + f'[[rule]]\npath = "~/dev/own/{{client}}/**"\nbillable = false\n'
                                   f'\n[flowapp]\nurl = "{self.fake.url}"\n')
        conn = db.connect()
        t1 = local_ts(f"{self.day} 15:00")
        for off, kind in ((0, "prompt"), (600, "agent"), (1200, "stop")):
            db.record_event(conn, ts=t1 + off, session_id="s4dddddd", kind=kind, cwd=own, project=own, branch="main")
            db.touch_session(conn, "s4dddddd", t1 + off, project_dir=own)
        conn.close()

        def keys(billable):
            _, body = self.get(f"/api/flowapp/preview?from={self.day}&to={self.day}&billable={billable}")
            return {(t["key"], b["status"]) for d in body["days"] for t in d["tasks"] for b in t["blocks"]}
        everything = keys(0)
        (unbilled,) = {k for k, _ in everything} - {"12345"}
        _, body = self.get(f"/api/flowapp/preview?from={self.day}&to={self.day}&billable=0")
        ext = next(b["external_id"] for d in body["days"] for t in d["tasks"] if t["key"] == unbilled
                   for b in t["blocks"])
        status, _ = self.post("/api/flowapp/draft", {"day": self.day, "external_id": ext, "action": "skip"})
        self.assertEqual(status, 200)
        self.assertIn((unbilled, "skipped"), keys(0))
        self.assertEqual(keys(1), {("12345", "new")})

    def test_resend_only_for_entries_deleted_in_flowapp(self):
        status, body = self.post("/api/flowapp/draft", {"day": self.day, "external_id": self.ext, "action": "resend"})
        self.assertEqual(status, 400)

    def test_send_then_the_entry_is_sent_once(self):
        status, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        self.assertEqual((status, set(body)), (200, {"job_id"}))
        snap = self.wait_for(body["job_id"])
        self.assertEqual(set(snap), {"id", "running", "total", "done", "failed", "cancelled", "errors", "fatal"})
        self.assertEqual((snap["total"], snap["done"], snap["failed"], snap["fatal"]), (1, 1, 0, None))
        self.fake.entries = list(self.fake.stored.values())
        block = self.preview()["days"][0]["tasks"][0]["blocks"][0]
        self.assertEqual((block["status"], block["flowapp_id"], block["export_status"]), ("sent", "101", "none"))
        # Selecting it again sends nothing: it is refused before the job.
        status, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        snap = self.wait_for(body["job_id"])
        self.assertEqual((snap["done"], snap["failed"], snap["errors"][0]["code"]), (1, 1, "status_sent"))
        self.assertEqual(len(self.fake.writes()), 1)

    def test_deleted_in_flowapp_then_resent_on_request(self):
        _, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        self.wait_for(body["job_id"])
        self.fake.entries = []                      # deleted by hand in FlowApp
        block = self.preview()["days"][0]["tasks"][0]["blocks"][0]
        self.assertEqual(block["status"], "deleted_remote")
        status, body = self.post("/api/flowapp/draft", {"day": self.day, "external_id": self.ext, "action": "resend"})
        self.assertEqual((status, body["task"]["blocks"][0]["status"]), (200, "new"))

    def test_one_send_at_a_time(self):
        class Running:
            id = "busy"

            def snapshot(self):
                return {"running": True}

            def cancel(self):
                pass

            def join(self, timeout=None):
                pass

        dashboard._current_job = Running()
        status, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        self.assertEqual((status, body["job_id"]), (409, "busy"))
        dashboard._current_job = None

    def test_cancel_and_unknown_job(self):
        _, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        status, answer = self.post("/api/flowapp/job/cancel", {"id": body["job_id"]})
        self.assertEqual((status, answer), (200, {"ok": True}))
        self.wait_for(body["job_id"])
        self.assertEqual(self.get("/api/flowapp/job?id=nope")[0], 404)
        self.assertEqual(self.post("/api/flowapp/job/cancel", {"id": "nope"})[0], 404)

    def test_writes_need_the_header(self):
        for path, body in (("/api/flowapp/draft", {"day": self.day, "external_id": self.ext, "action": "skip"}),
                           ("/api/flowapp/send", {"external_ids": [self.ext]}),
                           ("/api/flowapp/job/cancel", {"id": "x"})):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, body, header=False)[0], 403)
        self.assertEqual(self.fake.writes(), [])

    def test_reads_check_the_host(self):
        for path in ("/api/flowapp/status", "/api/flowapp/preview", "/api/flowapp/job?id=x", "/flowapp"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, headers={"Host": "evil.example:80"})[0], 403)

    def test_no_route_ever_answers_with_the_token(self):
        # get()/post() assert the token is absent from every body; walk every route once.
        self.get("/api/flowapp/status")
        self.preview()
        self.post("/api/flowapp/draft", {"day": self.day, "external_id": self.ext, "action": "skip"})
        self.post("/api/flowapp/draft", {"day": self.day, "external_id": self.ext, "action": "unskip"})
        _, body = self.post("/api/flowapp/send", {"external_ids": [self.ext]})
        self.wait_for(body["job_id"])
        self.post("/api/flowapp/job/cancel", {"id": body["job_id"]})
        status, _, raw = self.request("/flowapp")
        self.assertNotIn(FLOWAPP_TOKEN.encode(), raw)

    def test_the_page_is_served_when_present(self):
        import tempfile
        saved = dashboard.FLOWAPP_PAGE
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
            f.write("<html>flowapp</html>")
        try:
            dashboard.FLOWAPP_PAGE = dashboard.Path(f.name)
            status, ctype, raw = self.request("/flowapp")
            self.assertEqual((status, ctype, raw), (200, "text/html; charset=utf-8", b"<html>flowapp</html>"))
            dashboard.FLOWAPP_PAGE = dashboard.Path(f.name + ".missing")
            self.assertEqual(self.request("/flowapp")[0], 404)
        finally:
            dashboard.FLOWAPP_PAGE = saved
            os.unlink(f.name)
