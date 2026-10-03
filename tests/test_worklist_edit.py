"""Changing a task's status and moving it to another project (and so client)."""

from tracker import db, ledger, worklist
from tests.test_ledger import ACME, OWN, LedgerTestCase


class EditTestCase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        for slug, client, path in (("storefront", "acme", ACME), ("statusline", "own", OWN)):
            self.conn.execute("INSERT INTO projects (slug, client, path, created_at) VALUES (?, ?, ?, 0)",
                              (slug, client, path))
        self.own = worklist.project(self.conn, "statusline")
        self.acme = worklist.project(self.conn, "storefront")

    def seconds(self, key):
        return round(worklist.time_of(self.conn, self.cfg, key)[0])


class StatusTest(EditTestCase):
    def setUp(self):
        super().setUp()
        self.key = worklist.add(self.conn, self.own, "Fix rounding", now=self.t0)
        self.decision = worklist.add(self.conn, self.own, "Keep VAT in the API", now=self.t0,
                                     kind="decision", status="in-force")

    def row(self, key=None):
        return db.task_details(self.conn, key or self.key)

    def test_done_needs_an_outcome_and_stamps_closed_at(self):
        with self.assertRaises(ValueError):
            worklist.set_status(self.conn, self.key, "done")
        worklist.set_status(self.conn, self.key, "done", "rounded half up", now=self.t0 + 60, author="dashboard")
        r = self.row()
        self.assertEqual((r["status"], r["outcome"], r["closed_at"]), ("done", "rounded half up", self.t0 + 60))
        last = worklist.history(self.conn, self.key)[-1]
        self.assertEqual((last["author"], last["field"]), ("dashboard", "outcome"))

    def test_reopening_clears_closed_at(self):
        worklist.set_status(self.conn, self.key, "parked", now=self.t0 + 60)
        self.assertEqual(self.row()["closed_at"], self.t0 + 60)
        worklist.set_status(self.conn, self.key, "in-progress", now=self.t0 + 120)
        self.assertEqual((self.row()["status"], self.row()["closed_at"]), ("in-progress", None))

    def test_a_status_must_fit_the_kind(self):
        with self.assertRaises(ValueError):
            worklist.set_status(self.conn, self.key, "in-force")
        with self.assertRaises(ValueError):
            worklist.set_status(self.conn, self.decision, "done", "x")
        worklist.set_status(self.conn, self.decision, "revoked", now=self.t0 + 60)
        self.assertEqual((self.row(self.decision)["status"], self.row(self.decision)["closed_at"]),
                         ("revoked", self.t0 + 60))

    def test_unknown_task(self):
        with self.assertRaises(LookupError):
            worklist.set_status(self.conn, "own:statusline#99", "open")


class MoveTest(EditTestCase):
    def setUp(self):
        super().setUp()
        worklist.add(self.conn, self.acme, "Earlier acme task", now=self.t0)          # acme:storefront#1
        self.key = worklist.add(self.conn, self.own, "Pricing export", now=self.t0, ticket="SHOP-7")
        self.other = worklist.add(self.conn, self.own, "Ship it", now=self.t0, depends_on=f'["{self.key}"]')
        worklist.note(self.conn, self.key, "started", now=self.t0)
        worklist.metric(self.conn, self.key, "rows", "0", "12", "SQL count", now=self.t0)
        self.busy("s1", 0, 1200, cwd=OWN)
        db.add_assignment(self.conn, "s1", self.key, self.t0, "cli")

    def test_the_task_and_everything_on_it_follow(self):
        before = self.seconds(self.key)
        new = worklist.move(self.conn, self.key, "storefront", now=self.t0 + 7200, author="dashboard")
        self.assertEqual(new, "acme:storefront#2")
        self.assertIsNone(db.task_details(self.conn, self.key))
        r = db.task_details(self.conn, new)
        self.assertEqual((r["project"], r["number"], r["title"], r["ticket"]), ("storefront", 2, "Pricing export", "SHOP-7"))
        self.assertEqual([n["text"] for n in worklist.notes(self.conn, new)], ["started"])
        self.assertEqual([m["name"] for m in worklist.metrics(self.conn, new)], ["rows"])
        self.assertEqual(worklist.history(self.conn, new)[0]["field"], "created")
        self.assertEqual(worklist.history(self.conn, new)[-1]["field"], "moved")
        self.assertEqual(worklist.items(db.task_details(self.conn, self.other)["depends_on"]), [new])
        self.assertEqual(self.seconds(new), before)

    def test_the_time_goes_to_the_new_client(self):
        self.assertEqual(self.totals(by=("client", "task")), {("own", self.key): 1200})
        new = worklist.move(self.conn, self.key, "storefront")
        self.assertEqual(self.totals(by=("client", "billable", "task")), {("acme", True, new): 1200})

    def test_the_old_key_still_resolves(self):
        new = worklist.move(self.conn, self.key, "storefront")
        self.assertEqual(worklist.resolve(self.conn, self.key), new)
        # A second move: the first key follows to the latest one.
        newest = worklist.move(self.conn, new, "statusline")
        self.assertEqual(worklist.resolve(self.conn, self.key), newest)
        self.assertEqual(worklist.resolve(self.conn, new), newest)

    def test_moving_to_its_own_project_changes_nothing(self):
        self.assertEqual(worklist.move(self.conn, self.key, "statusline"), self.key)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM task_moves").fetchone()[0], 0)

    def test_unknown_project_or_task(self):
        with self.assertRaises(LookupError):
            worklist.move(self.conn, self.key, "nowhere")
        with self.assertRaises(LookupError):
            worklist.move(self.conn, "own:statusline#99", "storefront")
        self.assertIsNotNone(db.task_details(self.conn, self.key))
