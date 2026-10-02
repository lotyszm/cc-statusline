import unittest

from tracker.timeline import Event, allocate, credit_session, merge
from tests.helpers import IsolatedTestCase, local_ts

IDLE = 15 * 60
CAP = 60 * 60


def credited(events):
    return sum(end - start for start, end, _ in credit_session(events, IDLE, CAP))


class CreditSessionTest(unittest.TestCase):
    def test_gaps_within_idle_are_credited(self):
        events = [Event(0, "prompt"), Event(60, "tool_start", "Bash", "t1"),
                  Event(70, "tool_end", "Bash", "t1"), Event(300, "stop")]
        self.assertEqual(credited(events), 300)

    def test_gap_longer_than_idle_is_a_break(self):
        events = [Event(0, "prompt"), Event(100, "stop"),
                  Event(1100, "prompt"), Event(1200, "stop")]
        self.assertEqual(credited(events), 200)

    def test_interval_starts_at_the_earlier_event(self):
        events = [Event(0, "prompt"), Event(100, "stop"),
                  Event(1100, "prompt"), Event(1200, "stop")]
        spans = [(s, e) for s, e, _ in credit_session(events, IDLE, CAP)]
        self.assertEqual(spans, [(0, 100), (1100, 1200)])

    def test_long_running_tool_is_credited(self):
        events = [Event(0, "prompt"), Event(10, "tool_start", "Bash", "t1"),
                  Event(1810, "tool_end", "Bash", "t1"), Event(1820, "stop")]
        self.assertEqual(credited(events), 1820)

    def test_tool_running_longer_than_cap_is_capped(self):
        events = [Event(0, "tool_start", "Bash", "t1"), Event(7200, "tool_end", "Bash", "t1")]
        self.assertEqual(credited(events), 3600)

    def test_cap_counts_from_when_the_tool_started(self):
        events = [Event(0, "tool_start", "Agent", "t1"), Event(1000, "subagent_stop"),
                  Event(4000, "tool_end", "Agent", "t1")]
        self.assertEqual(credited(events), 3600)

    def test_interactive_tool_waits_for_the_user(self):
        events = [Event(0, "tool_start", "AskUserQuestion", "q1"),
                  Event(3000, "tool_end", "AskUserQuestion", "q1")]
        self.assertEqual(credited(events), 0)

    def test_permission_wait_stops_tool_credit(self):
        events = [Event(0, "tool_start", "Bash", "t1"), Event(1, "waiting"),
                  Event(3000, "tool_end", "Bash", "t1")]
        self.assertEqual(credited(events), 1)

    def test_tool_that_never_ended_is_not_credited_past_idle(self):
        events = [Event(0, "tool_start", "Bash", "t1"), Event(3000, "prompt")]
        self.assertEqual(credited(events), 0)

    def test_session_end_stops_credit(self):
        events = [Event(0, "stop"), Event(10, "session_end"), Event(20, "session_start")]
        self.assertEqual(credited(events), 10)

    def test_parallel_tools_keep_the_session_busy(self):
        events = [Event(0, "tool_start", "Bash", "a"), Event(1, "tool_start", "Read", "b"),
                  Event(2, "tool_end", "Read", "b"), Event(1802, "tool_end", "Bash", "a")]
        self.assertEqual(credited(events), 1802)

    def test_tool_end_recorded_before_its_start_is_ignored(self):
        events = [Event(5, "tool_end", "Read", "a"), Event(6, "tool_start", "Read", "a"),
                  Event(3000, "prompt")]
        self.assertEqual(credited(events), 1)


class MergeTest(unittest.TestCase):
    def test_touching_intervals_with_the_same_key_are_joined(self):
        got = merge([(0, 10, "a"), (10, 20, "a"), (20, 30, "b"), (40, 50, "b")])
        self.assertEqual(got, [(0, 20, "a"), (20, 30, "b"), (40, 50, "b")])


class AllocateTest(IsolatedTestCase):
    def test_split_shares_overlapping_time_between_sessions(self):
        t0 = local_ts("2026-09-15 10:00")
        got = allocate([(t0, t0 + 3600, "s1"), (t0 + 1800, t0 + 5400, "s2")], "split")
        self.assertEqual(got, {("s1", "2026-09-15"): 2700, ("s2", "2026-09-15"): 2700})

    def test_full_counts_every_session_completely(self):
        t0 = local_ts("2026-09-15 10:00")
        got = allocate([(t0, t0 + 3600, "s1"), (t0 + 1800, t0 + 5400, "s2")], "full")
        self.assertEqual(got, {("s1", "2026-09-15"): 3600, ("s2", "2026-09-15"): 3600})

    def test_priority_sessions_take_the_clock_first(self):
        t0 = local_ts("2026-09-15 10:00")
        got = allocate([(t0, t0 + 3600, "client"), (t0 + 1800, t0 + 5400, "own")], "split",
                       priority=lambda key: key == "client")
        self.assertEqual(got, {("client", "2026-09-15"): 3600, ("own", "2026-09-15"): 1800})

    def test_priority_sessions_still_share_among_themselves(self):
        t0 = local_ts("2026-09-15 10:00")
        got = allocate([(t0, t0 + 3600, "a"), (t0, t0 + 3600, "b"), (t0, t0 + 3600, "own")], "split",
                       priority=lambda key: key != "own")
        self.assertEqual(got, {("a", "2026-09-15"): 1800, ("b", "2026-09-15"): 1800})

    def test_intervals_are_cut_at_local_midnight(self):
        start = local_ts("2026-09-30 23:30")
        got = allocate([(start, start + 3600, "s1")], "split")
        self.assertEqual(got, {("s1", "2026-09-30"): 1800, ("s1", "2026-10-01"): 1800})


if __name__ == "__main__":
    unittest.main()
