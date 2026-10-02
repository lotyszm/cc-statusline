import contextlib
import io
import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.request

from tracker import dashboard, db, paths
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


class DashboardTest(IsolatedTestCase):
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
