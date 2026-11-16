from datetime import date, datetime, timezone

from tracker import config, db, flowapp
from tests.helpers import IsolatedTestCase, local_ts
from tests.test_flowapp_blocks import build, key, span

DAY = "2026-11-10"
TODAY = date(2026, 11, 16)
NOW = local_ts("2026-11-16 12:00")
OTHER = key(task="d24:cms#118")


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def entry(row, **over):
    """FlowApp's copy of a sent row, as GET /tracker/entries returns it."""
    e = {"id": row["flowapp_id"], "startedAt": utc(row["sent_start"]), "stoppedAt": utc(row["sent_stop"]),
         "comment": row["sent_comment"], "redmineId": row["sent_redmine_id"], "externalId": row["external_id"],
         "exportStatus": "none", "source": "api"}
    e.update(over)
    return e


class ReconcileTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def exports(self):
        return flowapp.load_exports(self.conn, DAY, DAY)

    def view(self, blocks, remote=None, today=TODAY, now=NOW):
        return flowapp.reconcile(blocks, self.exports(), remote, today, now)

    def send(self, block, flowapp_id="101"):
        """Store a block as sent, the way the send job does."""
        flowapp.save_draft(self.conn, block, "edit", {}, NOW)
        flowapp.mark_sent(self.conn, block.external_id,
                          {"id": flowapp_id, "startedAt": utc(block.start), "stoppedAt": utc(block.stop),
                           "comment": block.comment, "redmineId": block.redmine_id,
                           "externalId": block.external_id}, NOW)
        return self.exports()[block.external_id]

    def test_untouched_blocks_are_new(self):
        blocks = build([span(DAY, "09:00", "09:34")])
        (it,) = self.view(blocks)
        self.assertEqual((it["status"], it["flags"], it["start"], it["stop"]),
                         ("new", [], blocks[0].start, blocks[0].stop))

    def test_sent_row_shows_stored_values(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        flowapp.save_draft(self.conn, b, "edit", {"comment": "Icons, pass 2"}, NOW)
        (it,) = self.view([b])
        self.assertEqual((it["status"], it["comment"], it["flowapp_id"]), ("changed", "Icons, pass 2", "101"))
        flowapp.save_draft(self.conn, b, "edit", {"comment": b.comment}, NOW)
        self.assertEqual(self.view([b])[0]["status"], "sent")

    def test_grown_sent_block_is_hidden_and_flagged(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        # Later events glue on: the tracker now sees 09:00-09:50, the same moment and more.
        grown = build([span(DAY, "09:00", "09:34"), span(DAY, "09:40", "09:50")])
        (it,) = self.view(grown)
        self.assertEqual((it["external_id"], it["status"], it["flags"]), (b.external_id, "sent", ["tracker_changed"]))
        self.assertEqual(it["tracked_stop"], local_ts(f"{DAY} 09:50"))
        self.assertEqual(it["stop"], b.stop)

    def test_shifted_start_still_matches_the_sent_block(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        shifted = build([span(DAY, "08:57", "09:34")])
        self.assertNotEqual(shifted[0].external_id, b.external_id)
        (it,) = self.view(shifted)
        self.assertEqual((it["external_id"], it["status"], it["flags"]), (b.external_id, "sent", ["tracker_changed"]))

    def test_a_disjoint_part_is_a_new_block(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        items = self.view(build([span(DAY, "09:00", "09:34"), span(DAY, "11:00", "11:20")]))
        self.assertEqual([(i["status"], i["flags"]) for i in items], [("sent", []), ("new", [])])

    def test_time_moved_to_another_task_is_not_offered_again(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        moved = build([span(DAY, "09:00", "09:34", OTHER)])
        (it,) = self.view(moved)
        self.assertEqual((it["key"], it["status"], it["flags"]),
                         ("d24:cms#120", "sent", ["tracker_changed", "task_changed"]))

    def test_parallel_work_on_another_task_is_new_but_flagged(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        both = build([span(DAY, "09:00", "09:34"), span(DAY, "09:10", "09:40", OTHER)])
        items = {i["key"]: i for i in self.view(both)}
        self.assertEqual((items["d24:cms#120"]["status"], items["d24:cms#120"]["flags"]), ("sent", []))
        self.assertEqual((items["d24:cms#118"]["status"], items["d24:cms#118"]["flags"]), ("new", ["overlaps_sent"]))

    def test_shrunk_or_vanished_sent_block_is_flagged_not_shortened(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        (it,) = self.view([])
        self.assertEqual((it["status"], it["flags"], it["stop"], it["active_seconds"]),
                         ("sent", ["tracker_changed"], b.stop, 0.0))

    def test_entry_deleted_in_flowapp_is_never_new_again(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        (it,) = self.view([b], remote=[])
        self.assertEqual(it["status"], "deleted_remote")
        # Not even when FlowApp lists other entries, made by hand.
        (it,) = self.view([b], remote=[{"id": "9", "externalId": None, "startedAt": utc(b.start),
                                        "stoppedAt": utc(b.stop), "comment": "by hand"}])
        self.assertEqual(it["status"], "deleted_remote")

    def test_resend_of_a_deleted_entry_offers_it_as_new(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        flowapp.save_draft(self.conn, b, "resend", {}, NOW)
        (it,) = self.view([b], remote=[])
        self.assertEqual((it["status"], it["flowapp_id"]), ("new", None))

    def test_resend_keeps_edits_as_a_draft(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        flowapp.save_draft(self.conn, b, "edit", {"comment": "Edited"}, NOW)
        flowapp.save_draft(self.conn, b, "resend", {}, NOW)
        (it,) = self.view([b], remote=[])
        self.assertEqual((it["status"], it["comment"], it["flowapp_id"]), ("draft", "Edited", None))

    def test_exported_to_redmine(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        row = self.send(b)
        (it,) = self.view([b], remote=[entry(row, exportStatus="exported")])
        self.assertEqual((it["status"], it["flags"], it["export_status"]), ("sent", ["exported"], "exported"))
        (it,) = self.view([b], remote=[entry(row)])
        self.assertEqual((it["flags"], it["export_status"]), ([], "none"))

    def test_changed_by_hand_in_flowapp(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        row = self.send(b)
        for over in ({"stoppedAt": utc(b.stop + 600)}, {"comment": "typed in FlowApp"}):
            with self.subTest(over=over):
                (it,) = self.view([b], remote=[entry(row, **over)])
                self.assertEqual(it["flags"], ["changed_remote"])

    def test_found_by_external_id_when_ids_differ(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        row = self.send(b)
        (it,) = self.view([b], remote=[entry(row, id="other")])
        self.assertEqual((it["status"], it["flags"]), ("sent", []))

    def test_unreachable_flowapp_says_nothing_about_the_remote(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        self.send(b)
        (it,) = self.view([b], remote=None)
        self.assertEqual((it["status"], it["flags"], it["export_status"]), ("sent", [], None))

    def test_old_days_are_blocked(self):
        blocks = build([span(DAY, "09:00", "09:34")])
        late = local_ts(DAY + " 00:00") + flowapp.MAX_AGE_S + 1
        self.assertEqual(self.view(blocks, today=date(2027, 1, 11), now=late)[0]["status"], "blocked")
        self.assertEqual(self.view(blocks, today=date(2027, 1, 10), now=late - 2)[0]["status"], "new")

    def test_today_is_marked(self):
        blocks = build([span(DAY, "09:00", "09:34")])
        self.assertEqual(self.view(blocks, today=date(2026, 11, 10), now=local_ts(DAY + " 12:00"))[0]["status"],
                         "today")

    def test_draft_survives_a_recomputed_proposal(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        flowapp.save_draft(self.conn, b, "edit", {"comment": "Reviewed", "redmine_id": "31",
                                                  "stop": local_ts(DAY + " 09:45")}, NOW)
        for blocks in ([b], build([span(DAY, "09:03", "09:34")])):
            with self.subTest(start=blocks[0].start):
                (it,) = self.view(blocks)
                self.assertEqual((it["external_id"], it["status"], it["comment"], it["redmine_id"], it["stop"]),
                                 (b.external_id, "draft", "Reviewed", "31", local_ts(DAY + " 09:45")))
        (it,) = self.view(build([span(DAY, "09:03", "09:34")]))
        self.assertEqual(it["flags"], ["tracker_changed"])

    def test_skipped_blocks_do_not_come_back_as_new(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        flowapp.save_draft(self.conn, b, "skip", {}, NOW)
        (it,) = self.view(build([span(DAY, "09:02", "09:34")]))
        self.assertEqual((it["external_id"], it["status"]), (b.external_id, "skipped"))
        flowapp.save_draft(self.conn, b, "unskip", {}, NOW)
        self.assertEqual(self.view([b])[0]["status"], "draft")

    def test_merge_with_the_next_block(self):
        first, second = build([span(DAY, "09:00", "09:34"), span(DAY, "11:00", "11:20")])
        flowapp.save_draft(self.conn, first, "merge_next", {"next": second}, NOW)
        (it,) = self.view([first, second])
        self.assertEqual((it["external_id"], it["status"], it["start"], it["stop"]),
                         (first.external_id, "draft", first.start, second.stop))

    def test_merged_entry_counts_both_blocks_of_the_tracker(self):
        first, second = build([span(DAY, "09:00", "09:34"), span(DAY, "11:00", "11:20")])
        before = flowapp.days_json(self.view([first, second]), {})[0]["tracked_seconds"]
        flowapp.save_draft(self.conn, first, "merge_next", {"next": second}, NOW)
        (it,) = self.view([first, second])
        self.assertEqual((it["flags"], it["tracked_seconds"], it["active_seconds"]), ([], 54 * 60, 54 * 60))
        day = flowapp.days_json([it], {})[0]
        self.assertEqual((before, day["tracked_seconds"]), (54 * 60, 54 * 60))
        self.assertEqual(day["tasks"][0]["blocks"][0]["tracked_seconds"], 54 * 60)
        # Sent, the merged entry still owns both blocks: no "tracker changed".
        row = self.exports()[first.external_id]
        flowapp.mark_sent(self.conn, first.external_id,
                          {"id": "101", "startedAt": utc(row["start"]), "stoppedAt": utc(row["stop"]),
                           "comment": row["comment"], "externalId": first.external_id}, NOW)
        (it,) = self.view([first, second])
        self.assertEqual((it["status"], it["flags"], it["tracked_seconds"]), ("sent", [], 54 * 60))

    def test_billable_view_leaves_out_stored_rows_of_unbilled_time(self):
        cfg = config.Config({"rule": [{"path": "/w/own/**", "client": "own", "billable": False},
                                      {"path": "/w/{client}/**"}]})
        own = key(task="own:claude#9", client="own", billable=False, project="/w/own/claude")
        billed, unbilled = build([span(DAY, "09:00", "09:34"), span(DAY, "11:00", "11:20", own)],
                                 billable_only=False)
        for b in (billed, unbilled):
            flowapp.save_draft(self.conn, b, "skip", {}, NOW)
        kept = flowapp.billable_exports(self.exports(), [billed, unbilled], cfg)
        self.assertEqual(set(kept), {billed.external_id})
        # With the tracker's blocks gone, the task key's client decides.
        self.assertEqual(set(flowapp.billable_exports(self.exports(), [], cfg)), {billed.external_id})

    def test_sent_blocks_cannot_be_skipped_or_merged(self):
        first, second = build([span(DAY, "09:00", "09:34"), span(DAY, "11:00", "11:20")])
        self.send(first)
        with self.assertRaises(ValueError):
            flowapp.save_draft(self.conn, first, "skip", {}, NOW)
        with self.assertRaises(ValueError):
            flowapp.save_draft(self.conn, first, "merge_next", {"next": second}, NOW)

    def test_edited_times_are_whole_minutes(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        flowapp.save_draft(self.conn, b, "edit", {"start": local_ts(f"{DAY} 09:01") + 30,
                                                  "stop": local_ts(f"{DAY} 09:40") + 1}, NOW)
        row = self.exports()[b.external_id]
        self.assertEqual((row["start"], row["stop"]), (local_ts(f"{DAY} 09:01"), local_ts(f"{DAY} 09:41")))

    def test_invalid_edits_are_refused(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        for fields in ({"stop": b.start}, {"stop": b.start + 25 * 3600},
                       {"start": local_ts("2026-11-09 23:00")}, {"redmine_id": "CMS-1"}, {"comment": " "}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                flowapp.save_draft(self.conn, b, "edit", fields, NOW)

    def test_replayed_create_keeps_what_flowapp_holds(self):
        b = build([span(DAY, "09:00", "09:34")])[0]
        flowapp.save_draft(self.conn, b, "edit", {"comment": "New text"}, NOW)
        # The entry was made earlier with the old comment; the replay answers with it.
        flowapp.mark_sent(self.conn, b.external_id, {"id": 7, "startedAt": utc(b.start), "stoppedAt": utc(b.stop),
                                                     "comment": b.comment, "externalId": b.external_id}, NOW)
        row = self.exports()[b.external_id]
        self.assertEqual((row["status"], row["flowapp_id"], row["sent_comment"]), ("changed", "7", b.comment))
