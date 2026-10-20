"""Subagents with tasks of their own: time per (session, agent) in full overlap mode."""

import io
import json
import os
import sqlite3

from tracker import cli, config, db, hook, ledger, paths, worklist
from tests.helpers import IsolatedTestCase
from tests.test_ledger import ACME, LedgerTestCase

MIN = 60


class AgentStreamsTest(LedgerTestCase):
    """A session that starts two background agents and waits while they work 10 minutes."""

    def setUp(self):
        super().setUp()
        self.add("s1", 0, "prompt")
        self.add("s1", 20, "tool_start", tool="Agent", tool_use_id="spawn-1")
        self.add("s1", 25, "tool_end", tool="Agent", tool_use_id="spawn-1")
        self.add("s1", 30, "tool_start", tool="Agent", tool_use_id="spawn-2")
        self.add("s1", 35, "tool_end", tool="Agent", tool_use_id="spawn-2")
        self.add("s1", 40, "stop")
        for agent in ("a1", "a2"):
            self.agent(agent, 60, 60 + 10 * MIN)

    def agent(self, agent, start, end):
        self.add("s1", start, "subagent_start", agent_id=agent)
        for t in range(start + 120, end, 120):
            self.add("s1", t, "tool_start", tool="Bash", tool_use_id=f"{agent}-{t}", agent_id=agent)
            self.add("s1", t + 5, "tool_end", tool="Bash", tool_use_id=f"{agent}-{t}", agent_id=agent)
        self.add("s1", end, "subagent_stop", agent_id=agent)

    def pin(self, agent, task, at=60):
        db.add_assignment(self.conn, "s1", task, self.t0 + at, "test", agent_id=agent)

    def test_two_agents_on_two_tasks_each_get_their_full_time(self):
        # Before: everything went to the session, the agents' tasks got nothing.
        self.pin("a1", "acme:A")
        self.pin("a2", "acme:B")
        self.assertEqual(self.totals(mode="full"), {("acme", "acme:A"): 600, ("acme", "acme:B"): 600,
                                                    ("acme", None): 60})

    def test_before_the_change_the_session_took_it_all(self):
        """What the same data gave without per-agent streams: the measured 'before'."""
        self.pin("a1", "acme:A")
        self.pin("a2", "acme:B")
        out = {}
        for s, e, k in ledger.intervals(self.conn, self.cfg, self.t0 - 3600, self.t0 + 6 * 3600, per_agent=False):
            out[k.task] = out.get(k.task, 0) + e - s
        self.assertEqual({k: round(v) for k, v in out.items()}, {None: 660})

    def test_an_agent_without_a_task_works_on_the_sessions(self):
        db.add_assignment(self.conn, "s1", "acme:S", self.t0, "test")
        self.pin("a2", "acme:B")
        # a1 stays in the session's stream: the session's task runs while it works.
        self.assertEqual(self.totals(mode="full"), {("acme", "acme:S"): 660, ("acme", "acme:B"): 600})

    def test_a_task_shared_by_session_and_agent_counts_once(self):
        db.add_assignment(self.conn, "s1", "acme:A", self.t0, "test")
        self.pin("a1", "acme:A")
        self.pin("a2", "acme:A")
        self.assertEqual(self.totals(mode="full"), {("acme", "acme:A"): 660})

    def test_an_agent_pinned_mid_way_moves_from_then_on(self):
        self.pin("a1", "acme:A", at=60 + 5 * MIN)
        self.pin("a2", "acme:B")
        got = self.totals(mode="full")
        self.assertEqual(got[("acme", "acme:B")], 600)
        self.assertEqual(got[("acme", "acme:A")], 300)
        self.assertEqual(got[("acme", None)], 360)        # until a1 left it, the session was at work

    def test_split_mode_is_untouched_by_agent_tasks(self):
        db.add_assignment(self.conn, "s1", "acme:S", self.t0, "test")
        self.busy("s2", 0, 10 * MIN, cwd=ACME)             # a parallel session sharing the clock
        before = self.totals(mode="split")
        self.pin("a1", "acme:A")
        self.pin("a2", "acme:B")
        self.assertEqual(self.totals(mode="split"), before)
        self.assertNotIn(("acme", "acme:A"), before)

    def test_full_mode_without_agent_tasks_is_as_before(self):
        db.add_assignment(self.conn, "s1", "acme:S", self.t0, "test")
        old = ledger.intervals(self.conn, self.cfg, self.t0 - 3600, self.t0 + 6 * 3600, per_agent=False)
        new = ledger.intervals(self.conn, self.cfg, self.t0 - 3600, self.t0 + 6 * 3600, per_agent=True)
        self.assertEqual(new, old)

    def test_the_status_line_and_task_history_keep_the_sessions_task(self):
        db.add_assignment(self.conn, "s1", "acme:S", self.t0, "test")
        self.pin("a1", "acme:A")
        self.assertEqual(ledger.current(self.conn, self.cfg, "s1", now=self.t0 + 700).task, "acme:S")
        self.assertEqual([t for _, t, _ in db.assignments(self.conn, ["s1"])["s1"]], ["acme:S"])


class MigrationTest(IsolatedTestCase):
    def test_an_old_database_gets_the_column_and_keeps_its_assignments(self):
        path = paths.db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE assignments (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, task TEXT,
                effective_from REAL NOT NULL, created_at REAL NOT NULL, origin TEXT NOT NULL, branch TEXT);
            INSERT INTO assignments (session_id, task, effective_from, created_at, origin, branch)
                VALUES ('s1', 'acme:A', 100, 100, 'cli', 'main');
            PRAGMA user_version = 6;
        """)
        old.close()
        conn = db.connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(assignments)")}
            self.assertIn("agent_id", cols)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(db.assignments(conn, ["s1"])["s1"], [(100, "acme:A", "main")])
            self.assertEqual(db.agent_assignments(conn, ["s1"]), {})
            db.add_assignment(conn, "s1", "acme:B", 200, "cli", agent_id="a1")
            self.assertEqual(db.agent_assignments(conn, ["s1"]), {("s1", "a1"): [(200, "acme:B", None)]})
            self.assertEqual(len(db.assignments(conn, ["s1"])["s1"]), 1)
        finally:
            conn.close()


class AutoPinTest(IsolatedTestCase):
    """The hook reads the key the coordinator put in the agent's description or prompt."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "clients", "acme", "shop")
        os.makedirs(os.path.join(self.repo, ".git"))
        with open(os.path.join(self.repo, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/main\n")
        self.write_config(f'[[rule]]\npath = "{self.tmp}/clients/{{client}}/**"\n')
        self.cfg = config.load()
        self.conn = db.connect()
        self.conn.execute("INSERT INTO projects (slug, client, path, created_at) VALUES ('cms', 'acme', ?, 0)",
                          (self.repo,))
        proj = worklist.project(self.conn, "cms")
        self.a = worklist.add(self.conn, proj, "Media library")
        self.b = worklist.add(self.conn, proj, "Dashboard")
        self.transcript = os.path.join(self.tmp, "projects", "p", "sess-1.jsonl")
        self.subagents = os.path.join(self.tmp, "projects", "p", "sess-1", "subagents")
        os.makedirs(self.subagents)
        self.now = 1_790_000_000.0

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def write_agent(self, agent, description=None, prompt=None):
        if description is not None:
            with open(os.path.join(self.subagents, f"agent-{agent}.meta.json"), "w", encoding="utf-8") as f:
                json.dump({"agentType": "dev", "description": description, "toolUseId": "toolu_1"}, f)
        if prompt is not None:
            with open(os.path.join(self.subagents, f"agent-{agent}.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"agentId": agent, "type": "user",
                                    "message": {"role": "user", "content": prompt}}) + "\n")
                f.write(json.dumps({"type": "assistant"}) + "\n")

    def fire(self, event, agent, dt=0, **extra):
        self.now += dt
        payload = {"session_id": "sess-1", "hook_event_name": event, "cwd": self.repo,
                   "transcript_path": self.transcript, "agent_id": agent, **extra}
        return hook.handle(payload, self.conn, self.cfg, self.now, {"CLAUDE_PROJECT_DIR": self.repo})

    def pinned(self):
        return {a: [(eff, t) for eff, t, _ in rows]
                for (_, a), rows in db.agent_assignments(self.conn, ["sess-1"]).items()}

    def test_key_in_the_description_pins_the_agent_from_its_start(self):
        self.write_agent("a1", description=f"DEV-CMS · {self.a} · media", prompt=f"Also see {self.b}.")
        out = self.fire("SubagentStart", "a1", agent_type="dev")
        self.assertEqual(self.pinned(), {"a1": [(self.now, self.a)]})
        self.assertIn(self.a, out["hookSpecificOutput"]["additionalContext"])

    def test_a_prompt_written_after_the_start_is_read_on_the_first_tool_call(self):
        start = self.now
        self.assertIsNone(self.fire("SubagentStart", "a1"))
        self.assertEqual(self.pinned(), {})
        self.write_agent("a1", prompt=f"Work on {self.b}: the dashboard columns.")
        self.fire("PreToolUse", "a1", dt=10, tool_name="Read", tool_use_id="t1")
        self.assertEqual(self.pinned(), {"a1": [(start, self.b)]})

    def test_only_the_first_line_of_the_prompt_counts(self):
        self.write_agent("a1", description="DEV · media", prompt=f"Repo: shop\n\nFor example {self.a}.")
        self.fire("SubagentStart", "a1")
        self.assertEqual(self.pinned(), {})
        self.write_agent("a2", description="DEV · media", prompt=f"\nTask {self.a}: the media library.\nMore.")
        self.fire("SubagentStart", "a2")
        self.assertEqual(self.pinned(), {"a2": [(self.now, self.a)]})

    def test_a_prompt_naming_several_tasks_pins_nothing(self):
        self.write_agent("a1", description="Explore", prompt=f"Compare {self.a} with {self.b}.")
        out = self.fire("SubagentStart", "a1")
        self.assertEqual(self.pinned(), {})
        self.assertIn("--agent a1", out["hookSpecificOutput"]["additionalContext"])
        # decided once: no rereading, and no pin later either
        self.write_agent("a1", description=f"now {self.a}")
        self.fire("PreToolUse", "a1", dt=5, tool_name="Read", tool_use_id="t1")
        self.assertEqual(self.pinned(), {})

    def test_unknown_keys_are_ignored(self):
        self.write_agent("a1", description="acme:cms#999 and cms#12345", prompt="nothing here")
        self.fire("SubagentStart", "a1")
        self.assertEqual(self.pinned(), {})

    def test_a_pin_by_hand_is_kept(self):
        self.fire("PreToolUse", "a1", tool_name="Read", tool_use_id="t0")
        db.add_assignment(self.conn, "sess-1", self.b, self.now, "cli", agent_id="a1")
        self.write_agent("a1", description=f"{self.a}", prompt="")
        self.fire("PreToolUse", "a1", dt=5, tool_name="Read", tool_use_id="t1")
        self.assertEqual([t for _, t in self.pinned()["a1"]], [self.b])

    def test_session_events_are_not_touched(self):
        self.fire("PreToolUse", None, tool_name="Read", tool_use_id="t0")
        self.assertEqual(self.pinned(), {})


class AgentCliTest(LedgerTestCase):
    def run_cli(self, *argv, now=None):
        out = io.StringIO()
        code = cli.main(list(argv), now=now or self.t0 + 7200, out=out)
        return code, out.getvalue()

    def test_set_task_for_an_agent_by_prefix(self):
        self.add("s1", 0, "prompt")
        self.add("s1", 100, "subagent_start", agent_id="a1b2c3")
        self.add("s1", 400, "tool_start", tool="Bash", tool_use_id="x", agent_id="a1b2c3")
        code, text = self.run_cli("task", "set", "acme:A", "--session", "s1", "--agent", "a1b")
        self.assertEqual(code, 0, text)
        self.assertEqual(db.agent_assignments(self.conn, ["s1"]),
                         {("s1", "a1b2c3"): [(self.t0 + 100, "acme:A", None)]})
        self.assertEqual(db.assignments(self.conn, ["s1"])["s1"], [])
        self.assertIn("agent a1b2c3", text)
        self.assertIn("overlap", text)                   # the test config runs split mode
        code, text = self.run_cli("task", "show", "--session", "s1")
        self.assertIn("agent a1b2c3", text)

    def test_unknown_agent_fails_without_writing(self):
        self.add("s1", 0, "prompt")
        code, _ = self.run_cli("task", "set", "acme:A", "--session", "s1", "--agent", "zz")
        self.assertEqual(code, 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM assignments").fetchone()[0], 0)
