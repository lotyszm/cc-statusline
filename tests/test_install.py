import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tracker import config, db, install, paths
from tests.helpers import IsolatedTestCase

CMD = '"/usr/bin/python3" "/repo/statusline.py" hook'

USER_SETTINGS = {
    "statusLine": {"type": "command", "command": "python3 /x/statusline.py"},
    "permissions": {"allow": ["Bash(npm test:*)"]},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/guard.sh"}]}]},
}


def ours(settings, event):
    return [h for group in settings.get("hooks", {}).get(event, []) for h in group["hooks"]
            if install.is_ours(h)]


class WireTest(unittest.TestCase):
    def test_every_event_gets_exactly_one_handler_even_when_run_twice(self):
        s = install.wire(install.wire(copy.deepcopy(USER_SETTINGS), CMD), CMD)
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                      "Notification", "Stop", "SubagentStop", "SessionEnd"):
            self.assertEqual(len(ours(s, event)), 1, event)

    def test_hooks_that_must_finish_are_synchronous_and_the_rest_async(self):
        # Context hooks return output; Stop must survive `claude -p` exiting
        # right after the reply, where async hooks are cancelled.
        s = install.wire(copy.deepcopy(USER_SETTINGS), CMD)
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "StopFailure"):
            self.assertNotIn("async", ours(s, event)[0], event)
        self.assertTrue(ours(s, "PreToolUse")[0]["async"])

    def test_existing_settings_are_kept(self):
        s = install.wire(copy.deepcopy(USER_SETTINGS), CMD)
        self.assertEqual(s["statusLine"], USER_SETTINGS["statusLine"])
        self.assertIn({"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/guard.sh"}]},
                      s["hooks"]["PreToolUse"])
        self.assertEqual(s["permissions"]["allow"], ["Bash(npm test:*)", "Bash(cc-statusline task:*)"])

    def test_unwire_restores_the_original_settings(self):
        s = install.unwire(install.wire(copy.deepcopy(USER_SETTINGS), CMD))
        self.assertEqual(s, USER_SETTINGS)

    def test_unwire_drops_sections_it_emptied(self):
        s = install.unwire(install.wire({}, CMD))
        self.assertEqual(s, {})

    def test_command_quotes_both_paths_and_is_recognised_as_ours(self):
        cmd = install.command("/opt/py 3/bin/python3", "/My Code/cc-statusline/statusline.py")
        self.assertEqual(cmd, '"/opt/py 3/bin/python3" "/My Code/cc-statusline/statusline.py" hook')
        self.assertTrue(install.is_ours({"type": "command", "command": cmd}))

    def test_other_hooks_are_not_ours(self):
        for cmd in ("/x/guard.sh", '"/usr/bin/python3" "/x/bin/agentd" hook', "statusline.py --doctor"):
            self.assertFalse(install.is_ours({"type": "command", "command": cmd}), cmd)

    def test_wired_events_and_the_hook_interpreter(self):
        s = install.wire(copy.deepcopy(USER_SETTINGS), CMD)
        self.assertEqual(install.wired_events(s), {event for event, _ in install.HOOKS})
        self.assertEqual(install.hook_python(s), "/usr/bin/python3")
        self.assertEqual(install.wired_events(USER_SETTINGS), set())
        self.assertIsNone(install.hook_python(USER_SETTINGS))


class StablePythonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-statusline-py-"))
        self.real = self.tmp / "Cellar" / "python@3.14" / "3.14.4" / "bin" / "python3.14"
        self.real.parent.mkdir(parents=True)
        self.real.write_text("")
        self.link = self.tmp / "bin" / "python3"
        self.link.parent.mkdir()
        self.link.symlink_to(self.real)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_stable_link_to_the_same_interpreter_is_preferred(self):
        # Homebrew reports a versioned path that an upgrade removes; the
        # bin/python3 link to it survives upgrades.
        self.assertEqual(install.stable_python(str(self.real), [str(self.link)]), str(self.link))

    def test_the_interpreter_itself_when_no_link_points_to_it(self):
        other = self.tmp / "other"
        other.write_text("")
        self.assertEqual(install.stable_python(str(self.real), [str(other), str(self.tmp / "missing")]),
                         str(self.real))


class SetupTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.bin_dir = os.path.join(self.tmp, "bin")
        self.link = os.path.join(self.bin_dir, "cc-statusline")

    def run_setup(self, **kw):
        out = io.StringIO()
        install.setup("/usr/bin/python3", bin_dir=self.bin_dir, out=out, **kw)
        return out.getvalue()

    def test_link_interpreter_and_config_template_are_created(self):
        self.run_setup()
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(install.SCRIPT.name, "statusline.py")
        self.assertEqual(os.path.realpath(self.link), str(install.SCRIPT.resolve()))
        self.assertEqual((paths.data_dir() / "python").read_text().strip(), "/usr/bin/python3")
        self.assertIn("idle_minutes", paths.config_path().read_text())

    def test_an_existing_config_is_not_overwritten(self):
        self.write_config("idle_minutes = 7\n")
        self.run_setup()
        self.assertEqual(paths.config_path().read_text(), "idle_minutes = 7\n")

    def test_a_file_that_is_not_a_link_is_left_alone(self):
        os.makedirs(self.bin_dir)
        with open(self.link, "w") as f:
            f.write("#!/bin/sh\n")
        out = self.run_setup()
        self.assertFalse(os.path.islink(self.link))
        self.assertIn("left alone", out)

    def test_no_link_when_asked(self):
        self.run_setup(link=False)
        self.assertFalse(os.path.lexists(self.link))

    def test_remove_link_removes_only_ours(self):
        self.run_setup()
        install.remove_link(bin_dir=self.bin_dir, out=io.StringIO())
        self.assertFalse(os.path.lexists(self.link))
        os.symlink("/usr/bin/true", self.link)
        install.remove_link(bin_dir=self.bin_dir, out=io.StringIO())
        self.assertTrue(os.path.islink(self.link))


class DoctorTest(IsolatedTestCase):
    def account(self, name, settings):
        acc = Path(self.tmp) / name
        acc.mkdir()
        (acc / "settings.json").write_text(json.dumps(settings))
        return acc

    def test_each_account_is_reported_and_an_unwired_one_fails_the_check(self):
        wired = self.account(".claude-a", install.wire(copy.deepcopy(USER_SETTINGS), CMD))
        bare = self.account(".claude-b", USER_SETTINGS)
        conn = db.connect()
        out = io.StringIO()
        try:
            ok = install.doctor(conn, config.load(), 1_790_000_000.0, [wired, bare], out=out)
        finally:
            conn.close()
        text = out.getvalue()
        self.assertFalse(ok)
        self.assertRegex(text, r"\.claude-a\s+wired")
        self.assertRegex(text, r"\.claude-b\s+not wired")


if __name__ == "__main__":
    unittest.main()
