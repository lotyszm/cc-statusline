import io
import json
import os
import re
import shlex
import unittest
from unittest import mock
from datetime import datetime

from tracker import config, db, hook, paths
from tests.helpers import IsolatedTestCase


class HookTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "clients", "acme", "shop")
        os.makedirs(os.path.join(self.repo, ".git"))
        os.makedirs(os.path.join(self.repo, "src"))
        self.set_branch("main")
        self.write_config(f'[[rule]]\npath = "{self.tmp}/clients/{{client}}/**"\n')
        self.cfg = config.load()
        self.conn = db.connect()
        self.now = 1_790_000_000.0

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def set_branch(self, name):
        with open(os.path.join(self.repo, ".git", "HEAD"), "w") as f:
            f.write(f"ref: refs/heads/{name}\n")

    def fire(self, event, sid="sess-1", cwd=None, dt=0, **extra):
        self.now += dt
        payload = {"session_id": sid, "hook_event_name": event, "cwd": cwd or self.repo,
                   "transcript_path": f"/Users/x/.claude/projects/p/{sid}.jsonl", **extra}
        return hook.handle(payload, self.conn, self.cfg, self.now, {"CLAUDE_PROJECT_DIR": self.repo})

    def context(self, out):
        return out["hookSpecificOutput"]["additionalContext"] if out else None


class RecordingTest(HookTestCase):
    def test_tool_event_is_stored_with_repo_and_branch(self):
        self.set_branch("feature/12345/hotfix")
        self.fire("PreToolUse", cwd=os.path.join(self.repo, "src"),
                  tool_name="Bash", tool_use_id="toolu_1")
        row = self.conn.execute("SELECT * FROM events").fetchone()
        self.assertEqual((row["kind"], row["project"], row["branch"], row["tool"], row["tool_use_id"]),
                         ("tool_start", self.repo, "feature/12345/hotfix", "Bash", "toolu_1"))

    def test_session_keeps_account_and_project_dir(self):
        self.fire("SessionStart", transcript_path="/Users/x/.claude-mc/projects/p/sess-1.jsonl")
        s = db.session(self.conn, "sess-1")
        self.assertEqual((s["account"], s["project_dir"]), ("claude-mc", self.repo))

    def test_subagent_events_carry_the_agent_id(self):
        self.fire("PostToolUse", tool_name="Read", tool_use_id="t2", agent_id="agent-7")
        self.assertEqual(self.conn.execute("SELECT agent_id FROM events").fetchone()[0], "agent-7")

    def test_idle_reminder_is_not_an_event(self):
        # It fires ~60 s after every reply, so recording it would credit a
        # phantom minute before each break.
        self.fire("Notification", notification_type="idle_prompt", message="Claude is waiting for your input")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def test_permission_prompt_is_a_wait(self):
        self.fire("Notification", notification_type="permission_prompt", message="Claude needs your permission")
        self.assertEqual(self.conn.execute("SELECT kind FROM events").fetchone()[0], "waiting")

    def test_payload_without_a_session_is_ignored(self):
        self.assertIsNone(hook.handle({"hook_event_name": "Stop"}, self.conn, self.cfg, self.now, {}))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 0)


class SessionStartTest(HookTestCase):
    def test_billable_project_gets_the_command_with_this_session(self):
        self.set_branch("feature/12345/hotfix")
        ctx = self.context(self.fire("SessionStart", source="startup"))
        self.assertIn("task set", ctx)
        self.assertIn("--session sess-1", ctx)
        self.assertIn("12345", ctx)

    def test_the_agent_is_told_to_use_cc_statusline_when_it_is_on_path(self):
        with mock.patch.object(hook.shutil, "which", return_value="/home/u/.local/bin/cc-statusline"):
            ctx = self.context(self.fire("SessionStart", source="startup"))
        self.assertIn("cc-statusline task set <TASK-ID> --session sess-1", ctx)

    def test_without_the_command_on_path_the_agent_gets_the_script_itself(self):
        with mock.patch.object(hook.shutil, "which", return_value=None):
            ctx = self.context(self.fire("SessionStart", source="startup"))
        self.assertTrue(str(hook.SCRIPT).endswith("statusline.py"))
        self.assertIn(f"{shlex.quote(str(hook.SCRIPT))} task set <TASK-ID>", ctx)

    def test_other_projects_get_no_context(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        payload = {"session_id": "s9", "hook_event_name": "SessionStart", "cwd": other}
        self.assertIsNone(hook.handle(payload, self.conn, self.cfg, self.now, {"CLAUDE_PROJECT_DIR": other}))


class PromptTest(HookTestCase):
    def test_new_task_id_makes_the_agent_ask_once(self):
        first = self.context(self.fire("UserPromptSubmit", prompt="popraw zadanie 12345"))
        self.assertIn("12345", first)
        self.assertIn("--session sess-1", first)
        self.assertIsNone(self.fire("UserPromptSubmit", dt=60, prompt="jeszcze raz zadanie 12345"))

    def test_mentioning_the_current_task_does_not_ask(self):
        self.set_branch("feature/12345/hotfix")
        self.assertIsNone(self.fire("UserPromptSubmit", prompt="kontynuuj zadanie 12345"))

    def test_switching_task_logs_from_the_prompt(self):
        self.set_branch("feature/12345/hotfix")
        ctx = self.context(self.fire("UserPromptSubmit", prompt="teraz SHOP-42"))
        self.assertIn(f"--since {self.now:.3f}", ctx)

    def test_since_never_points_after_the_prompt(self):
        self.set_branch("feature/12345/hotfix")
        self.now = 1_790_000_000.4567                   # rounding would land after the prompt
        ctx = self.context(self.fire("UserPromptSubmit", prompt="teraz SHOP-42"))
        since = float(re.search(r"--since (\S+)", ctx).group(1))
        prompt_ts = self.conn.execute("SELECT ts FROM events WHERE kind = 'prompt'").fetchone()[0]
        self.assertLessEqual(since, prompt_ts)

    def test_first_task_of_a_session_logs_from_the_start(self):
        ctx = self.context(self.fire("UserPromptSubmit", prompt="teraz SHOP-42"))
        self.assertNotIn("--since", ctx)

    def test_noise_in_a_prompt_does_not_ask(self):
        self.assertIsNone(self.fire("UserPromptSubmit", prompt="zgodnie z F3-40, UTF-8"))

    def test_first_prompt_becomes_the_session_title(self):
        self.fire("UserPromptSubmit", prompt="Napraw przekierowanie Przelewy\nszczegóły...")
        self.fire("UserPromptSubmit", dt=5, prompt="a teraz coś innego")
        self.assertEqual(db.session(self.conn, "sess-1")["title"], "Napraw przekierowanie Przelewy")


class StopTest(HookTestCase):
    def status(self):
        with open(paths.status_dir() / "sess-1.json") as f:
            return json.load(f)

    def test_stop_writes_the_status_file(self):
        self.set_branch("feature/12345/hotfix")
        self.fire("UserPromptSubmit", prompt="start")
        self.fire("Stop", dt=600)
        st = self.status()
        self.assertEqual((st["task"], st["billable"], round(st["seconds"])), ("12345", True, 600))
        self.assertEqual(st["day"], datetime.fromtimestamp(self.now).date().isoformat())

    def test_tool_activity_refreshes_a_stale_status_at_most_once_a_minute(self):
        self.fire("UserPromptSubmit", prompt="start")
        self.fire("PostToolUse", dt=30, tool_name="Bash", tool_use_id="t1")
        first = self.status()["updated"]
        self.fire("PostToolUse", dt=20, tool_name="Bash", tool_use_id="t2")
        self.assertEqual(self.status()["updated"], first)
        self.fire("PostToolUse", dt=50, tool_name="Bash", tool_use_id="t3")
        self.assertEqual(self.status()["updated"], self.now)

    def test_ai_title_from_the_transcript_replaces_the_prompt_title(self):
        transcript = os.path.join(self.tmp, "t.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
            f.write(json.dumps({"type": "ai-title", "aiTitle": "Old", "sessionId": "sess-1"}) + "\n")
            f.write(json.dumps({"type": "ai-title", "aiTitle": "Fix Przelewy redirect", "sessionId": "sess-1"}) + "\n")
        self.fire("UserPromptSubmit", prompt="napraw to")
        self.fire("Stop", dt=30, transcript_path=transcript)
        self.assertEqual(db.session(self.conn, "sess-1")["title"], "Fix Przelewy redirect")


class MainTest(IsolatedTestCase):
    def test_garbage_on_stdin_exits_cleanly_without_output(self):
        out = io.StringIO()
        self.assertEqual(hook.main(stdin=io.StringIO("not json"), stdout=out), 0)
        self.assertEqual(out.getvalue(), "")

    def test_main_records_the_event_and_prints_context(self):
        os.makedirs(os.path.join(self.tmp, "clients", "acme"))
        self.write_config(f'[[rule]]\npath = "{self.tmp}/clients/{{client}}/**"\n')
        payload = {"session_id": "m1", "hook_event_name": "SessionStart",
                   "cwd": os.path.join(self.tmp, "clients", "acme")}
        out = io.StringIO()
        self.assertEqual(hook.main(stdin=io.StringIO(json.dumps(payload)), stdout=out), 0)
        self.assertIn("--session m1", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])
        conn = db.connect()
        self.assertEqual(conn.execute("SELECT kind FROM events").fetchone()[0], "session_start")
        conn.close()


if __name__ == "__main__":
    unittest.main()
