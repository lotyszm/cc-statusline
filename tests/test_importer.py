import json
import os
import unittest

from tracker import config, db, importer, ledger
from tests.helpers import IsolatedTestCase, local_ts


def entry(kind, ts, uuid, content, sid="S1", cwd="/x/shop", branch="feature/12345/x", **extra):
    return {"type": kind, "timestamp": ts, "sessionId": sid, "uuid": uuid, "cwd": cwd,
            "gitBranch": branch, "isSidechain": False, "message": {"content": content}, **extra}


SESSION = [
    entry("user", "2026-09-15T08:00:00.000Z", "u1", "napraw przekierowanie"),
    entry("assistant", "2026-09-15T08:01:00.000Z", "u2",
          [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]),
    entry("user", "2026-09-15T08:21:00.000Z", "u3",
          [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "passed"}]),
    entry("assistant", "2026-09-15T08:22:00.000Z", "u4", [{"type": "text", "text": "gotowe"}]),
    {"type": "ai-title", "aiTitle": "Fix Przelewy redirect", "sessionId": "S1"},
]


class ImporterTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.account = os.path.join(self.tmp, ".claude-mc")
        self.project = os.path.join(self.account, "projects", "-x-shop")
        os.makedirs(self.project)
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def write(self, rel, entries, garbage=False):
        path = os.path.join(self.project, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            for i, e in enumerate(entries):
                f.write(json.dumps(e) + "\n")
                if garbage and i == 1:
                    f.write("{not json\n")
        return path

    def kinds(self):
        return [(r["kind"], r["tool"], r["tool_use_id"]) for r in
                self.conn.execute("SELECT * FROM events ORDER BY ts, id")]

    def test_entries_become_events(self):
        self.write("S1.jsonl", SESSION)
        importer.run(self.conn, [self.account])
        self.assertEqual(self.kinds(), [("prompt", None, None), ("tool_start", "Bash", "toolu_1"),
                                        ("tool_end", None, "toolu_1"), ("agent", None, None)])

    def test_imported_time_follows_the_same_rules(self):
        self.write("S1.jsonl", SESSION)
        importer.run(self.conn, [self.account])
        alloc = ledger.allocate(self.conn, config.load(), local_ts("2026-09-15 00:00"),
                                local_ts("2026-09-16 00:00"))
        self.assertEqual(round(sum(alloc.values())), 22 * 60)

    def test_session_gets_account_project_and_generated_title(self):
        self.write("S1.jsonl", SESSION)
        importer.run(self.conn, [self.account])
        s = db.session(self.conn, "S1")
        self.assertEqual((s["account"], s["project_dir"], s["title"]),
                         ("claude-mc", "/x/shop", "Fix Przelewy redirect"))
        self.assertEqual((s["first_ts"], s["last_ts"]),
                         (local_ts("2026-09-15 10:00"), local_ts("2026-09-15 10:22")))

    def test_running_twice_adds_nothing(self):
        path = self.write("S1.jsonl", SESSION)
        first = importer.run(self.conn, [self.account])
        os.utime(path, (1, 1))                        # force a re-read of the same content
        second = importer.run(self.conn, [self.account])
        self.assertEqual((first["events"], second["events"]), (4, 0))
        self.assertEqual(len(self.kinds()), 4)

    def test_unchanged_files_are_not_read_again(self):
        self.write("S1.jsonl", SESSION)
        importer.run(self.conn, [self.account])
        self.assertEqual(importer.run(self.conn, [self.account])["skipped_files"], 1)

    def test_sessions_already_tracked_by_hooks_are_left_alone(self):
        db.record_event(self.conn, ts=1.0, session_id="S1", kind="prompt")
        self.write("S1.jsonl", SESSION)
        stats = importer.run(self.conn, [self.account])
        self.assertEqual((stats["events"], stats["skipped_sessions"]), (0, 1))

    def test_history_from_before_the_hooks_started_is_still_imported(self):
        hooks_from = local_ts("2026-09-15 10:21")             # installed mid-session
        db.record_event(self.conn, ts=hooks_from, session_id="S1", kind="tool_end")
        self.write("S1.jsonl", SESSION)
        importer.run(self.conn, [self.account])
        got = [(r["kind"], r["source"]) for r in self.conn.execute("SELECT * FROM events ORDER BY ts, id")]
        self.assertEqual(got, [("prompt", "import"), ("tool_start", "import"), ("tool_end", "hook")])

    def test_force_reads_unchanged_files_again(self):
        self.write("S1.jsonl", SESSION)
        importer.run(self.conn, [self.account])
        stats = importer.run(self.conn, [self.account], force=True)
        self.assertEqual((stats["files"], stats["skipped_files"], stats["events"]), (1, 0, 0))

    def test_subagent_transcripts_count_for_the_parent_session(self):
        self.write("S1.jsonl", SESSION[:1])
        side = entry("assistant", "2026-09-15T08:05:00.000Z", "a1",
                     [{"type": "tool_use", "id": "toolu_9", "name": "Read", "input": {}}],
                     isSidechain=True, agentId="agent-7")
        self.write(os.path.join("S1", "subagents", "agent-7.jsonl"), [side])
        importer.run(self.conn, [self.account])
        row = self.conn.execute("SELECT session_id, agent_id FROM events WHERE kind = 'tool_start'").fetchone()
        self.assertEqual(tuple(row), ("S1", "agent-7"))

    def test_workflow_agents_nested_deeper_count_too(self):
        self.write("S1.jsonl", SESSION[:1])
        side = entry("assistant", "2026-09-15T08:05:00.000Z", "w1", [{"type": "text", "text": "x"}],
                     isSidechain=True, agentId="agent-wf")
        self.write(os.path.join("S1", "subagents", "workflows", "wf_1", "agent-wf.jsonl"), [side])
        importer.run(self.conn, [self.account])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE agent_id = 'agent-wf'")
                         .fetchone()[0], 1)

    def test_broken_lines_are_skipped(self):
        self.write("S1.jsonl", SESSION, garbage=True)
        self.assertEqual(importer.run(self.conn, [self.account])["events"], 4)

    def test_backup_folder_with_several_accounts(self):
        self.write("S1.jsonl", SESSION)
        importer.run(self.conn, [self.tmp])
        self.assertEqual(len(self.kinds()), 4)


if __name__ == "__main__":
    unittest.main()
