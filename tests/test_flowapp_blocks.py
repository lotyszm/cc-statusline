from datetime import date

from tracker import config, db, flowapp, ledger
from tests.helpers import IsolatedTestCase, local_ts

MIN = 60


def key(task="d24:cms#120", client="d24", billable=True, session="s1", project="/w/d24/cms"):
    return ledger.Key(session, client, billable, project, "main", task)


def span(day, start, end, k=None):
    """An interval from 'HH:MM' to 'HH:MM' (seconds allowed as HH:MM:SS) on `day`."""
    def ts(t):
        parts = t.split(":")
        base = local_ts(f"{day} {parts[0]}:{parts[1]}")
        return base + (int(parts[2]) if len(parts) > 2 else 0)
    return (ts(start), ts(end), k or key())


def build(intervals, gap=15, step=5, titles=None, tickets=None, billable_only=True):
    return flowapp.build_blocks(intervals, gap_s=gap * MIN, step_s=step * MIN, titles=titles or {},
                                tickets=tickets or {}, billable_only=billable_only)


def minutes(b):
    return (b.stop - b.start) / MIN


class BuildBlocksTest(IsolatedTestCase):
    def test_spec_example_rounds_each_block_up(self):
        d = "2026-11-10"
        blocks = build([span(d, "09:00", "09:34"), span(d, "10:00", "10:01"),
                        span(d, "11:00", "11:20"), span(d, "13:00", "13:08")])
        self.assertEqual([minutes(b) for b in blocks], [35, 5, 20, 10])
        self.assertEqual(sum(minutes(b) for b in blocks), 70)
        self.assertEqual([(b.tracked_stop - b.tracked_start) / MIN for b in blocks], [34, 1, 20, 8])
        self.assertTrue(all(b.start == b.tracked_start for b in blocks))

    def test_gap_up_to_the_threshold_is_glued(self):
        d = "2026-11-10"
        glued = build([span(d, "09:00", "09:10"), span(d, "09:24", "09:30")])
        self.assertEqual([(minutes(b), b.active_seconds / MIN) for b in glued], [(30, 16)])
        exactly = build([span(d, "09:00", "09:10"), span(d, "09:25", "09:30")])
        self.assertEqual([minutes(b) for b in exactly], [30])
        apart = build([span(d, "09:00", "09:10"), span(d, "09:26", "09:30")])
        self.assertEqual([minutes(b) for b in apart], [10, 5])

    def test_rounding_into_the_next_block_merges_them(self):
        d = "2026-11-10"
        # With no gluing, 09:00-09:02 rounds to 09:05 and so reaches the block at 09:04.
        blocks = build([span(d, "09:00", "09:02"), span(d, "09:04", "09:07"), span(d, "09:30", "09:31")],
                       gap=0, step=5)
        self.assertEqual([(minutes(b), b.active_seconds / MIN) for b in blocks], [(10, 5), (5, 1)])
        self.assertEqual(blocks[0].tracked_stop, span(d, "09:04", "09:07")[1])

    def test_a_block_never_crosses_midnight(self):
        blocks = build([(local_ts("2026-11-10 23:50"), local_ts("2026-11-11 00:20"), key())])
        self.assertEqual([(b.day, minutes(b)) for b in blocks], [("2026-11-10", 10), ("2026-11-11", 20)])
        clipped = build([(local_ts("2026-11-10 23:52"), local_ts("2026-11-10 23:58"), key())], step=15)
        self.assertEqual(clipped[0].stop, local_ts("2026-11-11 00:00"))

    def test_dst_day_splits_at_local_midnight(self):
        # 26.10.2026 is not a DST day in Warsaw (25.10 is); check the night of the change.
        blocks = build([(local_ts("2026-10-24 23:30"), local_ts("2026-10-25 04:00"), key())])
        self.assertEqual([b.day for b in blocks], ["2026-10-24", "2026-10-25"])
        self.assertEqual(blocks[1].start, local_ts("2026-10-25 00:00"))
        # 00:00-04:00 local on the night the clock goes back is five real hours.
        self.assertEqual(blocks[1].stop - blocks[1].start, 5 * 3600)

    def test_non_billable_time_is_left_out_unless_asked(self):
        own = key(task="own:claude#9", client="own", billable=False)
        d = "2026-11-10"
        ivs = [span(d, "09:00", "09:30"), span(d, "10:00", "10:30", own)]
        self.assertEqual([b.key for b in build(ivs)], ["d24:cms#120"])
        self.assertEqual([b.key for b in build(ivs, billable_only=False)], ["d24:cms#120", "own:claude#9"])

    def test_comment_and_redmine_id_come_from_the_task(self):
        d = "2026-11-10"
        other = key(task="d24:cms#118")
        third = key(task="d24:cms#7")
        b = build([span(d, "09:00", "09:30"), span(d, "10:00", "10:10", other), span(d, "11:00", "11:10", third)],
                  titles={"d24:cms#120": "Toolbar icons", "d24:cms#118": "Modal verdict"},
                  tickets={"d24:cms#120": "31", "d24:cms#118": "CMS-706"})
        got = {x.key: (x.comment, x.redmine_id) for x in b}
        self.assertEqual(got, {"d24:cms#120": ("Toolbar icons", "31"), "d24:cms#118": ("Modal verdict", None),
                               "d24:cms#7": ("d24:cms#7", None)})

    def test_time_without_a_task_goes_to_the_project(self):
        d = "2026-11-10"
        b = build([span(d, "09:00", "09:30", key(task=None, project="/w/d24/storefront"))])
        self.assertEqual((b[0].key, b[0].task, b[0].client, b[0].comment),
                         ("d24/storefront", None, "d24", "d24/storefront"))

    def test_two_sessions_on_one_task_count_once(self):
        d = "2026-11-10"
        b = build([span(d, "09:00", "09:40"), span(d, "09:20", "10:00", key(session="s2"))])
        self.assertEqual([(minutes(x), x.active_seconds / MIN) for x in b], [(60, 60)])

    def test_parallel_tasks_overlap_freely(self):
        d = "2026-11-10"
        b = build([span(d, "09:00", "10:00"), span(d, "09:00", "10:00", key(task="d24:cms#118"))])
        self.assertEqual(sorted((x.key, minutes(x)) for x in b), [("d24:cms#118", 60), ("d24:cms#120", 60)])

    def test_external_id_is_task_and_tracked_start(self):
        d = "2026-11-10"
        b = build([span(d, "09:12:30", "09:30")])[0]
        self.assertEqual(b.external_id, f"cc:d24:cms#120:{int(local_ts('2026-11-10 09:12')) + 30}")
        self.assertEqual(b.start, int(b.start))
        self.assertEqual((b.stop - b.start) % (5 * MIN), 0)


class CollectBlocksTest(IsolatedTestCase):
    def test_from_the_database_with_titles_tickets_and_workers(self):
        self.write_config('subagents = "workers"\n[[rule]]\npath = "/w/{client}/**"\n'
                          '[flowapp]\nurl = "https://e.test"\n')
        conn = db.connect()
        t0 = local_ts("2026-11-10 09:00")
        for off, kind in ((0, "prompt"), (600, "agent"), (1200, "agent"), (1800, "stop")):
            db.record_event(conn, ts=t0 + off, session_id="s1", kind=kind, cwd="/w/d24/cms", project="/w/d24/cms",
                            branch="main")
            db.touch_session(conn, "s1", t0 + off, project_dir="/w/d24/cms")
        db.add_assignment(conn, "s1", "d24:cms#120", t0, "test", t0)
        db.set_task(conn, "d24:cms#120", title="Toolbar icons", ticket="31")
        cfg = config.load()
        blocks = flowapp.collect_blocks(conn, cfg, date(2026, 11, 10), date(2026, 11, 10))
        conn.close()
        self.assertEqual([(b.key, b.comment, b.redmine_id, (b.stop - b.start) / MIN) for b in blocks],
                         [("d24:cms#120", "Toolbar icons", "31", 30)])
