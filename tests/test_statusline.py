"""statusline.py as the entry point: install, uninstall, doctor, the ⏱ hint,
the tracker commands and the hook. Everything runs against a throwaway HOME,
with the price fetch and the service manager stubbed out."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

from tracker import db, install, paths
from tests.helpers import IsolatedTestCase

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "statusline.py"
ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")


def load(path=SCRIPT):
    spec = importlib.util.spec_from_file_location("statusline_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StatuslineTestCase(IsolatedTestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)          # restores HOME and the rest afterwards
        env.start()
        self.addCleanup(env.stop)
        super().setUp()
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_CACHE_HOME"] = str(Path(self.tmp) / "cache")
        for k in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            os.environ.pop(k, None)
        self.acc = self.home / ".claude"
        self.acc.mkdir()
        (self.acc / "settings.json").write_text(json.dumps({"theme": "dark"}))
        self.sl = self.load(SCRIPT)
        for target, kw in (("tracker.autostart.enable", {"return_value": True}),
                           ("tracker.autostart.disable", {"return_value": True}),
                           ("tracker.autostart.status", {"return_value": {"manager": "launchd", "installed": True,
                                                                           "running": True, "path": None}}),
                           ("tracker.dashboard.answers", {"return_value": True})):
            patcher = mock.patch(target, **kw)
            setattr(self, target.rsplit(".", 1)[1], patcher.start())
            self.addCleanup(patcher.stop)

    def load(self, path):
        module = load(path)
        patcher = mock.patch.object(module, "fetch_prices", return_value=(0, []))
        patcher.start()
        self.addCleanup(patcher.stop)
        return module

    def solo(self):
        """statusline.py downloaded on its own, without tracker/."""
        target = Path(self.tmp) / "solo" / "statusline.py"
        target.parent.mkdir()
        shutil.copy2(SCRIPT, target)
        return self.load(target)

    def settings(self):
        return json.loads((self.acc / "settings.json").read_text())

    def run_main(self, *argv, module=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = (module or self.sl).main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def render(self, session_id="sess-1", module=None):
        module = module or self.sl
        module.set_config_dir(self.acc)
        data = {"session_id": session_id, "model": {"display_name": "Opus"},
                "workspace": {"current_dir": self.tmp},
                "context_window": {"used_percentage": 10, "total_input_tokens": 1000,
                                   "context_window_size": 200000}}
        return ANSI.sub("", module.render(data))


class InstallTest(StatuslineTestCase):
    def test_install_wires_the_status_line_and_time_tracking(self):
        code, out, _ = self.run_main("--install", "--no-import")
        self.assertEqual(code, 0, out)
        s = self.settings()
        self.assertEqual(s["statusLine"]["command"], f'python3 "{os.path.realpath(SCRIPT)}"')
        self.assertEqual(s["theme"], "dark")
        self.assertEqual(install.wired_events(s), {event for event, _ in install.HOOKS})
        self.assertIn("Bash(cc-statusline task:*)", s["permissions"]["allow"])
        link = self.home / ".local" / "bin" / "cc-statusline"
        self.assertEqual(os.path.realpath(link), os.path.realpath(SCRIPT))
        self.assertTrue(paths.config_path().exists())
        self.enable.assert_called_once()
        self.assertEqual(len(list(self.acc.glob("settings.json.bak-*"))), 1)

    def test_a_second_install_changes_nothing(self):
        self.run_main("--install", "--no-import")
        code, out, _ = self.run_main("--install", "--no-import")
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(len(list(self.acc.glob("settings.json.bak-*"))), 1)

    def test_hooks_run_with_the_interpreter_given(self):
        self.run_main("--install", "--no-import", "--python=/usr/bin/python3")
        self.assertEqual(install.hook_python(self.settings()), "/usr/bin/python3")

    def test_no_dashboard_turns_the_autostart_off(self):
        self.run_main("--install", "--no-import", "--no-dashboard")
        self.enable.assert_not_called()
        self.disable.assert_called_once()

    def test_no_tracking_leaves_only_the_status_line(self):
        self.run_main("--install", "--no-import")
        code, out, _ = self.run_main("--install", "--no-tracking")
        s = self.settings()
        self.assertEqual(install.wired_events(s), set())
        self.assertNotIn("permissions", s)
        self.assertIn("statusLine", s)
        self.assertTrue((paths.data_dir() / "no-tracking").exists())
        self.disable.assert_called()
        self.run_main("--install", "--no-import")
        self.assertFalse((paths.data_dir() / "no-tracking").exists())

    def test_install_imports_the_transcripts_claude_code_kept(self):
        project = self.acc / "projects" / "-tmp-x"
        project.mkdir(parents=True)
        entries = [
            {"type": "user", "timestamp": "2026-09-15T08:00:00.000Z", "sessionId": "s-1", "uuid": "u1",
             "cwd": "/tmp/x", "message": {"content": "hi"}},
            {"type": "assistant", "timestamp": "2026-09-15T08:05:00.000Z", "sessionId": "s-1", "uuid": "u2",
             "cwd": "/tmp/x", "message": {"content": [{"type": "text", "text": "ok"}]}},
        ]
        (project / "s-1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
        code, out, _ = self.run_main("--install")
        self.assertEqual(code, 0)
        self.assertIn("history", out)
        conn = db.connect()
        self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE session_id = 's-1'").fetchone()[0], 2)
        conn.close()

    def test_statusline_py_alone_installs_the_status_line_and_says_how_to_get_tracking(self):
        solo = self.solo()
        code, out, _ = self.run_main("--install", module=solo)
        self.assertEqual(code, 0)
        s = self.settings()
        self.assertEqual(s["statusLine"]["command"], f'python3 "{os.path.realpath(solo.__file__)}"')
        self.assertNotIn("hooks", s)
        self.assertIn("git clone", out)


class UninstallTest(StatuslineTestCase):
    def test_uninstall_removes_what_install_added_and_keeps_the_data(self):
        self.run_main("--install", "--no-import")
        code, out, _ = self.run_main("--uninstall")
        self.assertEqual(code, 0)
        self.assertEqual(self.settings(), {"theme": "dark"})
        self.assertFalse(os.path.lexists(self.home / ".local" / "bin" / "cc-statusline"))
        self.disable.assert_called()
        self.assertTrue(paths.data_dir().exists())

    def test_a_foreign_status_line_is_left_alone(self):
        (self.acc / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "x"}}))
        self.run_main("--uninstall")
        self.assertEqual(self.settings(), {"statusLine": {"type": "command", "command": "x"}})


class HintTest(StatuslineTestCase):
    def test_after_an_update_the_line_asks_for_install(self):
        self.assertIn("⏱ run --install", self.render())

    def test_no_hint_once_the_hooks_are_wired(self):
        self.run_main("--install", "--no-import")
        self.assertNotIn("run --install", self.render())

    def test_no_hint_after_opting_out(self):
        self.run_main("--install", "--no-tracking")
        self.assertNotIn("run --install", self.render())

    def test_no_hint_without_tracker(self):
        self.assertNotIn("run --install", self.render(module=self.solo()))

    def test_a_tracked_session_shows_its_task_and_time_today(self):
        status = paths.status_dir()
        status.mkdir(parents=True)
        (status / "sess-1.json").write_text(json.dumps({
            "seconds": 3720, "task": "12345", "billable": True, "day": datetime.now().strftime("%Y-%m-%d")}))
        line = self.render()
        self.assertIn("⏱ 12345 1h 2m", line)
        self.assertNotIn("run --install", line)

    def write_open(self, count=5):
        status = paths.status_dir()
        status.mkdir(parents=True, exist_ok=True)
        items = [{"task": f"acme:shop#{n}", "number": n, "title": f"Task number {n} " + "x" * 80,
                  "status": "in-progress" if n == 1 else "open", "priority": "risk" if n == 2 else "medium"}
                 for n in range(1, count + 1)]
        (status / "sess-1.json").write_text(json.dumps({
            "seconds": 60, "task": "acme:shop#1", "billable": True, "day": datetime.now().strftime("%Y-%m-%d"),
            "open": {"project": "shop", "count": count, "items": items}}))

    def test_a_wide_terminal_shows_the_open_tasks_beside_the_gauges(self):
        self.write_open()
        with mock.patch.dict(os.environ, {"COLUMNS": "150"}):
            lines = self.render().split("\n")
        self.assertIn("todo 5 #1 ▸ Task number 1", lines[1])
        self.assertIn("#2 · Task number 2", lines[2])
        self.assertIn("+2", lines[3])
        self.assertTrue(all(len(line) <= 148 for line in lines[1:]), [len(x) for x in lines])
        self.assertTrue(lines[1].endswith("…"))

    def test_a_narrow_or_unknown_terminal_leaves_the_tasks_out(self):
        self.write_open()
        for width in (80, 0):
            with mock.patch.object(self.sl, "terminal_width", return_value=width):
                self.assertNotIn("todo", self.render())

    def test_the_width_comes_from_columns_then_from_the_terminal(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "123"}):
            self.assertEqual(self.sl.terminal_width(), 123)
        with mock.patch.dict(os.environ, {"COLUMNS": "", "CC_STATUSLINE_COLUMNS": ""}), \
                mock.patch.object(self.sl.os, "open", side_effect=OSError):
            self.assertEqual(self.sl.terminal_width(), 0)
            os.environ["CC_STATUSLINE_COLUMNS"] = "180"
            self.assertEqual(self.sl.terminal_width(), 180)

    def test_control_characters_in_a_title_never_reach_the_terminal(self):
        self.write_open(count=1)
        f = paths.status_dir() / "sess-1.json"
        st = json.loads(f.read_text())
        st["open"]["items"][0]["title"] = "evil\x1b]0;pwned\x07\x9b2J end"
        f.write_text(json.dumps(st))
        with mock.patch.dict(os.environ, {"COLUMNS": "150"}):
            raw = self.sl.render({"session_id": "sess-1", "model": {"display_name": "Opus"},
                                  "workspace": {"current_dir": self.tmp}, "context_window": {}})
        task_part = raw.split("\n")[1].split("#1", 1)[1]
        self.assertNotRegex(ANSI.sub("", task_part), r"[\x00-\x1f\x7f-\x9f]")

    def test_a_project_with_nothing_open_says_so(self):
        self.write_open(count=0)
        with mock.patch.dict(os.environ, {"COLUMNS": "150"}):
            self.assertIn("todo nothing open", self.render())

    def test_odd_session_ids_stay_inside_the_status_directory(self):
        outside = paths.data_dir() / "x.json"
        outside.parent.mkdir(parents=True)
        outside.write_text(json.dumps({"seconds": 60, "task": "LEAK-1"}))
        self.assertNotIn("LEAK-1", self.render(session_id="../x"))


class CommandsTest(StatuslineTestCase):
    def test_tracker_commands_are_handed_over(self):
        code, out, _ = self.run_main("report", "--today", "--format", "csv")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("client,"), out)
        code, _, err = self.run_main("task", "show", "--session", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no session matches", err)

    def test_without_tracker_the_commands_say_how_to_get_it(self):
        code, _, err = self.run_main("report", module=self.solo())
        self.assertEqual(code, 1)
        self.assertIn("git clone", err)

    def test_help_instead_of_waiting_for_input_in_a_terminal(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with mock.patch.object(sys, "stdin", Terminal()):
            code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("--install", out)


class DoctorTest(StatuslineTestCase):
    def test_doctor_covers_time_tracking_and_the_dashboard(self):
        self.run_main("--install", "--no-import")
        _, out, _ = self.run_main("--doctor")
        self.assertRegex(out, r"hooks\s+\S+\.claude\s+wired")
        self.assertRegex(out, r"dashboard\s+http://127\.0\.0\.1:8765/ answering")

    def test_doctor_without_tracker_says_how_to_get_it(self):
        _, out, _ = self.run_main("--doctor", module=self.solo())
        self.assertIn("git clone", out)


class HookEntryTest(StatuslineTestCase):
    def run_hook(self, stdin):
        return subprocess.run([sys.executable, str(SCRIPT), "hook"], input=stdin, capture_output=True,
                              text=True, timeout=30, env=os.environ.copy())

    def test_claude_code_hooks_are_recorded_through_statusline_py(self):
        payload = {"session_id": "s-hook", "hook_event_name": "PreToolUse", "cwd": self.tmp,
                   "tool_name": "Bash", "tool_use_id": "t1"}
        r = self.run_hook(json.dumps(payload))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        conn = db.connect()
        self.assertEqual(conn.execute("SELECT kind, tool FROM events WHERE session_id = 's-hook'").fetchall()[0][:],
                         ("tool_start", "Bash"))
        conn.close()

    def test_a_broken_payload_never_fails_the_session(self):
        r = self.run_hook("not json")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
