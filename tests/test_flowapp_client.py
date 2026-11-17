import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tracker import db, flowapp
from tests.helpers import IsolatedTestCase, Redirector, local_ts
from tests.test_flowapp_blocks import build, span
from tests.test_flowapp_reconcile import utc

DAY = "2026-11-10"
NOW = local_ts("2026-11-16 12:00")
TOKEN = "flw_test_secret_token"


class FakeFlowApp:
    """A FlowApp stand-in on a free port: answers from a script, keeps every request.

    `script` is a list of (status, body, headers) used in order for writes;
    with the script empty a create answers 201 with the entry it was given.
    """

    def __init__(self):
        self.requests, self.script, self.entries = [], [], []
        self.get_error = None           # (status, body): the answer to every GET while set
        self.stored = {}
        self.next_id = 100
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def _record(self, body):
                fake.requests.append({"method": self.command, "path": self.path, "body": body,
                                      "auth": self.headers.get("Authorization")})

            def do_GET(self):
                self._record(None)
                url = urlparse(self.path)
                if fake.get_error:
                    return self._answer(*fake.get_error)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._answer(401, {"error": {"code": "unauthorized", "message": "Brak autoryzacji"}})
                if url.path.endswith("/me"):
                    return self._answer(200, {"name": "Test User", "expiresAt": "2027-02-14T09:01:53.417Z",
                                              "scopes": ["tracker:read", "tracker:write"]})
                if url.path.endswith("/tracker/entries"):
                    q = parse_qs(url.query)
                    return self._answer(200, {"data": [e for e in fake.entries
                                                       if q["from"][0] <= e["startedAt"][:10] <= q["to"][0]]})
                self._answer(404, {"error": {"code": "not_found", "message": "Nie znaleziono"}})

            def _write(self):
                body = self._body()
                self._record(body)
                if fake.script:
                    status, answer, headers = fake.script.pop(0)
                    if answer == "echo":
                        answer = fake.make(body)
                    return self._answer(status, answer, headers)
                if self.command == "POST":
                    return self._answer(201, fake.make(body))
                entry = fake.stored.get(self.path.rsplit("/", 1)[1])
                if entry is None:
                    return self._answer(404, {"error": {"code": "not_found", "message": "Nie znaleziono"}})
                entry.update({k: v for k, v in body.items() if k != "exportComment"})
                return self._answer(200, entry)

            do_POST = do_PATCH = _write

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def make(self, body):
        self.next_id += 1
        e = {"id": self.next_id, "startedAt": body["startedAt"], "stoppedAt": body["stoppedAt"],
             "comment": body["comment"], "redmineId": body.get("redmineId"), "externalId": body.get("externalId"),
             "exportStatus": "none", "source": "api"}
        self.stored[str(e["id"])] = e
        return dict(e)

    def writes(self):
        return [r for r in self.requests if r["method"] in ("POST", "PATCH")]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeTime:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


class ClientTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeFlowApp()
        self.client = flowapp.Client(self.fake.url, TOKEN, timeout=5)

    def tearDown(self):
        self.fake.close()
        super().tearDown()

    def test_me_and_bearer_token(self):
        self.assertEqual(self.client.me()["name"], "Test User")
        self.assertEqual(self.fake.requests[0]["auth"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, repr(self.client))

    def test_bad_token_is_401(self):
        with self.assertRaises(flowapp.ApiError) as cm:
            flowapp.Client(self.fake.url, "flw_wrong").me()
        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.message),
                         (401, "unauthorized", "Brak autoryzacji"))

    def test_nobody_listening_is_unreachable(self):
        url = self.fake.url
        self.fake.close()
        with self.assertRaises(flowapp.ApiError) as cm:
            flowapp.Client(url, TOKEN, timeout=2).me()
        self.assertEqual((cm.exception.status, cm.exception.code), (0, "unreachable"))
        self.fake = FakeFlowApp()

    def test_entries_are_asked_for_in_chunks_of_93_days(self):
        self.client.entries(date(2026, 1, 1), date(2026, 6, 30))
        ranges = [parse_qs(urlparse(r["path"]).query) for r in self.fake.requests]
        self.assertEqual([(q["from"][0], q["to"][0]) for q in ranges],
                         [("2026-01-01", "2026-04-03"), ("2026-04-04", "2026-06-30")])

    def test_create_tells_a_replay(self):
        self.fake.script = [(201, "echo", {}), (200, "echo", {"Idempotent-Replay": "true"})]
        payload = {"startedAt": "2026-11-10T09:00:00+01:00", "stoppedAt": "2026-11-10T09:35:00+01:00",
                   "comment": "x", "externalId": "cc:a:1"}
        self.assertFalse(self.client.create(payload)[1])
        self.assertTrue(self.client.create(payload)[1])

    def test_error_body_with_field(self):
        self.fake.script = [(400, {"error": {"code": "invalid_request", "message": "Nieznane zadanie",
                                             "field": "redmineId"}}, {})]
        with self.assertRaises(flowapp.ApiError) as cm:
            self.client.create({"comment": "x"})
        self.assertEqual((cm.exception.code, cm.exception.field), ("invalid_request", "redmineId"))

    def test_a_redirect_is_an_error_and_the_token_does_not_follow_it(self):
        port = urlparse(self.fake.url).port
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                hop = Redirector(f"http://localhost:{port}", status)
                try:
                    client = flowapp.Client(hop.url, TOKEN, timeout=5)
                    for call in (client.me, lambda: client.create({"comment": "x", "externalId": "cc:a:1"})):
                        with self.assertRaises(flowapp.ApiError) as cm:
                            call()
                        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.message),
                                         (status, "redirected",
                                          f"redirected to localhost:{port}; set the final URL in config"))
                finally:
                    hop.close()
                self.assertEqual(len(hop.requests), 2)
        self.assertEqual(self.fake.requests, [])     # the token never reached the other host


class SendJobTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeFlowApp()
        self.client = flowapp.Client(self.fake.url, TOKEN, timeout=5)
        self.time = FakeTime()
        self.conn = db.connect()
        self.blocks = build([span(DAY, "09:00", "09:34"), span(DAY, "11:00", "11:20"), span(DAY, "13:00", "13:08")],
                            tickets={"d24:cms#120": "31"}, titles={"d24:cms#120": "Toolbar icons"})
        for b in self.blocks:
            flowapp.save_draft(self.conn, b, "edit", {}, NOW)
        self.ids = [b.external_id for b in self.blocks]

    def tearDown(self):
        self.conn.close()
        self.fake.close()
        super().tearDown()

    def run_job(self, ids=None, **kw):
        job = flowapp.SendJob(db.connect, self.client, ids or self.ids, sleep=self.time.sleep,
                              clock=self.time.clock, now=lambda: NOW, **kw)
        job.run()
        return job.snapshot()

    def row(self, ext):
        return flowapp.load_exports(self.conn, DAY, DAY)[ext]

    def test_created_entries_are_stored_as_sent(self):
        snap = self.run_job()
        self.assertEqual((snap["running"], snap["total"], snap["done"], snap["failed"], snap["fatal"]),
                         (False, 3, 3, 0, None))
        row = self.row(self.ids[0])
        self.assertEqual((row["status"], row["flowapp_id"], row["sent_comment"], row["sent_redmine_id"]),
                         ("sent", "101", "Toolbar icons", "31"))
        self.assertEqual((row["sent_start"], row["sent_stop"]), (self.blocks[0].start, self.blocks[0].stop))
        body = self.fake.writes()[0]["body"]
        self.assertEqual(body, {"startedAt": "2026-11-10T09:00:00+01:00", "stoppedAt": "2026-11-10T09:35:00+01:00",
                                "comment": "Toolbar icons", "redmineId": "31", "externalId": self.ids[0]})

    def test_a_replay_counts_as_sent(self):
        self.fake.script = [(200, "echo", {"Idempotent-Replay": "true"})]
        self.run_job(self.ids[:1])
        self.assertEqual(self.row(self.ids[0])["status"], "sent")

    def test_patch_sends_only_what_changed(self):
        self.run_job(self.ids[:1])
        flowapp.save_draft(self.conn, self.blocks[0], "edit", {"comment": "Icons, pass 2"}, NOW)
        self.assertEqual(self.row(self.ids[0])["status"], "changed")
        self.run_job(self.ids[:1])
        patch = self.fake.writes()[-1]
        self.assertEqual((patch["method"], patch["path"], patch["body"]),
                         ("PATCH", "/api/v1/tracker/entries/101", {"comment": "Icons, pass 2"}))
        self.assertEqual(self.row(self.ids[0])["status"], "sent")

    def test_patch_carries_the_reason_for_an_exported_entry(self):
        self.run_job(self.ids[:1])
        flowapp.save_draft(self.conn, self.blocks[0], "edit",
                           {"stop": self.blocks[0].stop + 300, "export_comment": "Longer"}, NOW)
        self.run_job(self.ids[:1])
        self.assertEqual(self.fake.writes()[-1]["body"],
                         {"stoppedAt": "2026-11-10T09:40:00+01:00", "exportComment": "Longer"})

    def test_429_waits_and_retries_the_same_entry(self):
        self.fake.script = [(429, {"error": {"code": "rate_limited", "message": "Za dużo"}}, {"Retry-After": "7"})]
        snap = self.run_job(self.ids[:1])
        self.assertEqual((snap["done"], snap["failed"]), (1, 0))
        self.assertEqual([w["method"] for w in self.fake.writes()], ["POST", "POST"])
        self.assertIn(7.0, self.time.sleeps)
        self.assertEqual(len(self.time.sleeps), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM flowapp_exports WHERE flowapp_id IS NOT NULL")
                         .fetchone()[0], 1)

    def test_401_stops_the_job_and_leaves_the_rest(self):
        self.fake.script = [(201, "echo", {}), (401, {"error": {"code": "unauthorized", "message": "Token wygasł"}}, {})]
        snap = self.run_job()
        self.assertEqual((snap["done"], snap["fatal"]), (1, {"code": "unauthorized", "message": "Token wygasł"}))
        self.assertEqual([self.row(i)["status"] for i in self.ids], ["sent", "draft", "draft"])

    def test_a_token_flowapp_repeats_never_reaches_the_job_or_the_database(self):
        echo = {"error": {"code": "invalid_request", "message": f"Bad header: Bearer {TOKEN}", "field": TOKEN}}
        self.fake.script = [(400, echo, {}), (401, {"error": {"code": "unauthorized", "message": f"Expired: {TOKEN}"}},
                                              {})]
        snap = self.run_job()
        self.assertNotIn(TOKEN, json.dumps(snap))
        self.assertIn("[token]", snap["errors"][0]["message"])
        self.assertEqual(snap["fatal"], {"code": "unauthorized", "message": "Expired: [token]"})
        stored = [tuple(r) for r in self.conn.execute("SELECT * FROM flowapp_exports")]
        self.assertNotIn(TOKEN, repr(stored))
        self.assertIn("[token]", self.row(self.ids[0])["error"])

    def test_missing_redmine_key_stops_the_job(self):
        self.fake.script = [(400, {"error": {"code": "redmine_key_missing", "message": "Brak klucza"}}, {})]
        snap = self.run_job()
        self.assertEqual((snap["done"], snap["fatal"]["code"]), (0, "redmine_key_missing"))
        self.assertEqual(len(self.fake.writes()), 1)

    def test_a_rejected_entry_keeps_the_message_and_the_rest_go_on(self):
        self.fake.script = [(400, {"error": {"code": "invalid_request", "message": "Nieznane zadanie Redmine",
                                             "field": "redmineId"}}, {})]
        snap = self.run_job()
        self.assertEqual((snap["done"], snap["failed"]), (3, 1))
        self.assertEqual(snap["errors"], [{"external_id": self.ids[0], "code": "invalid_request",
                                           "message": "Nieznane zadanie Redmine (redmineId)"}])
        row = self.row(self.ids[0])
        self.assertEqual((row["status"], row["error"]),
                         ("error", "invalid_request: Nieznane zadanie Redmine (redmineId)"))
        self.assertEqual([self.row(i)["status"] for i in self.ids[1:]], ["sent", "sent"])

    def test_cancel_stops_after_the_current_entry(self):
        job = flowapp.SendJob(db.connect, self.client, self.ids, sleep=lambda s: job.cancel(),
                              clock=self.time.clock, now=lambda: NOW)
        job.run()
        snap = job.snapshot()
        # The cancel lands in the pause after the first write: the second is not started.
        self.assertTrue(snap["cancelled"])
        self.assertEqual(snap["done"], 1)
        self.assertEqual(len(self.fake.writes()), 1)
        self.assertEqual([self.row(i)["status"] for i in self.ids], ["sent", "draft", "draft"])

    def test_writes_are_spaced_by_min_interval(self):
        self.run_job(min_interval=2.1)
        self.assertEqual(self.time.sleeps, [2.1, 2.1])

    def test_an_interrupted_send_replays_without_duplicates(self):
        # FlowApp has the entry but the answer never reached us: the row is still a draft.
        first = self.blocks[0]
        made = self.fake.make(flowapp.create_payload(self.row(first.external_id)))
        self.fake.script = [(200, made, {"Idempotent-Replay": "true"})]
        self.run_job(self.ids[:1])
        row = self.row(first.external_id)
        self.assertEqual((row["status"], row["flowapp_id"]), ("sent", str(made["id"])))

    def test_sent_and_skipped_rows_are_not_sent_again(self):
        self.run_job(self.ids[:1])
        flowapp.save_draft(self.conn, self.blocks[1], "skip", {}, NOW)
        snap = self.run_job(self.ids[:2])
        self.assertEqual(snap["done"], 2)
        self.assertEqual(len(self.fake.writes()), 1)

    def test_started_in_a_thread(self):
        job = flowapp.SendJob(db.connect, self.client, self.ids, min_interval=0).start()
        job.join(10)
        self.assertEqual(job.snapshot()["done"], 3)
        self.assertFalse(job.snapshot()["running"])

    def test_iso_times_carry_the_local_offset(self):
        self.assertEqual(flowapp.iso(local_ts("2026-10-24 12:00")), "2026-10-24T12:00:00+02:00")
        self.assertEqual(flowapp.iso(local_ts("2026-10-26 12:00") + 0.7), "2026-10-26T12:00:00+01:00")
        self.assertEqual(flowapp.parse_time(utc(local_ts("2026-10-26 12:00"))), local_ts("2026-10-26 12:00"))
