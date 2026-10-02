import io
import os
import plistlib
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tracker import autostart, paths
from tests.helpers import IsolatedTestCase

PYTHON = "/opt/homebrew/bin/python3"
SCRIPT = Path("/Users/me/My Code/cc-statusline/statusline.py")


class Runner:
    """Stands in for launchctl and systemctl: records calls, never runs them."""

    def __init__(self, codes=None):
        self.calls = []
        self.codes = codes or {}

    def __call__(self, cmd):
        self.calls.append(cmd)
        verb = cmd[1] if cmd[0] == "launchctl" else cmd[2]      # systemctl --user <verb>
        codes = self.codes.get(verb, [0])
        code = codes.pop(0) if len(codes) > 1 else codes[0]
        return subprocess.CompletedProcess(cmd, code, "", "" if code == 0 else "Input/output error")

    def verbs(self):
        return [c[1] if c[0] == "launchctl" else " ".join(c[1:]) for c in self.calls]


class AutostartTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("XDG_CONFIG_HOME", None)
        self.out = io.StringIO()


class FilesTest(AutostartTestCase):
    def test_the_launchd_job_runs_the_service_at_login_and_after_a_crash(self):
        job = plistlib.loads(autostart.launchd_plist(PYTHON, SCRIPT, 8765))
        self.assertEqual(job["Label"], autostart.LABEL)
        self.assertEqual(job["ProgramArguments"],
                         [PYTHON, str(SCRIPT), "dashboard", "--service", "--port", "8765"])
        self.assertTrue(job["RunAtLoad"])
        self.assertEqual(job["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(job["StandardErrorPath"], str(paths.data_dir() / "dashboard.log"))

    def test_the_service_sees_the_data_dir_and_config_the_installer_saw(self):
        job = plistlib.loads(autostart.launchd_plist(PYTHON, SCRIPT, 8765))
        self.assertEqual(job["EnvironmentVariables"]["CC_STATUSLINE_DATA_DIR"], os.environ["CC_STATUSLINE_DATA_DIR"])
        self.assertEqual(job["EnvironmentVariables"]["CC_STATUSLINE_CONFIG"], os.environ["CC_STATUSLINE_CONFIG"])

    def test_the_systemd_unit_quotes_paths_and_restarts_only_on_failure(self):
        unit = autostart.systemd_unit(PYTHON, Path("/home/me/100% code/statusline.py"), 8765)
        self.assertIn('ExecStart="/opt/homebrew/bin/python3" "/home/me/100%% code/statusline.py" '
                      '"dashboard" "--service" "--port" "8765"', unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn(f"StandardError=append:{paths.data_dir() / 'dashboard.log'}", unit)


class LaunchdTest(AutostartTestCase):
    def test_enable_writes_the_job_and_loads_it(self):
        run = Runner()
        self.assertTrue(autostart.enable(PYTHON, SCRIPT, run=run, out=self.out, system="Darwin"))
        plist = self.home / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist"
        self.assertTrue(plist.exists())
        self.assertEqual(run.verbs(), ["bootout", "bootstrap"])
        self.assertEqual(run.calls[1], ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        self.assertIn("http://127.0.0.1:8765/", self.out.getvalue())

    def test_bootstrap_is_retried_while_the_old_job_winds_down(self):
        run = Runner({"bootstrap": [5, 5, 0]})
        with mock.patch.object(autostart.time, "sleep"):
            self.assertTrue(autostart.enable(PYTHON, SCRIPT, run=run, out=self.out, system="Darwin"))
        self.assertEqual(run.verbs(), ["bootout", "bootstrap", "bootstrap", "bootstrap"])

    def test_a_failed_load_is_reported(self):
        run = Runner({"bootstrap": [5]})
        with mock.patch.object(autostart.time, "sleep"):
            self.assertFalse(autostart.enable(PYTHON, SCRIPT, run=run, out=self.out, system="Darwin"))
        self.assertIn("Input/output error", self.out.getvalue())

    def test_disable_unloads_and_removes_the_job(self):
        autostart.enable(PYTHON, SCRIPT, run=Runner(), out=self.out, system="Darwin")
        run = Runner()
        self.assertTrue(autostart.disable(run=run, out=self.out, system="Darwin"))
        self.assertEqual(run.verbs(), ["bootout"])
        self.assertFalse((self.home / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist").exists())

    def test_disable_without_a_job_does_nothing(self):
        run = Runner()
        self.assertFalse(autostart.disable(run=run, out=self.out, system="Darwin"))
        self.assertEqual(run.calls, [])

    def test_status(self):
        self.assertEqual(autostart.status(run=Runner(), system="Darwin")["installed"], False)
        autostart.enable(PYTHON, SCRIPT, run=Runner(), out=self.out, system="Darwin")
        st = autostart.status(run=Runner({"print": [113]}), system="Darwin")
        self.assertEqual((st["installed"], st["running"], st["manager"]), (True, False, "launchd"))


class SystemdTest(AutostartTestCase):
    def test_enable_writes_the_unit_and_starts_it(self):
        run = Runner()
        with mock.patch.object(autostart.shutil, "which", return_value="/usr/bin/systemctl"):
            self.assertTrue(autostart.enable(PYTHON, SCRIPT, run=run, out=self.out, system="Linux"))
        self.assertTrue((self.home / ".config" / "systemd" / "user" / autostart.UNIT).exists())
        self.assertEqual(run.verbs(), ["--user daemon-reload", f"--user enable {autostart.UNIT}",
                                       f"--user restart {autostart.UNIT}"])

    def test_without_systemd_the_manual_script_is_suggested(self):
        run = Runner()
        with mock.patch.object(autostart.shutil, "which", return_value=None):
            self.assertFalse(autostart.enable(PYTHON, SCRIPT, run=run, out=self.out, system="Linux"))
        self.assertEqual(run.calls, [])
        self.assertIn("dashboard.sh", self.out.getvalue())

    def test_disable_stops_and_removes_the_unit(self):
        with mock.patch.object(autostart.shutil, "which", return_value="/usr/bin/systemctl"):
            autostart.enable(PYTHON, SCRIPT, run=Runner(), out=self.out, system="Linux")
            run = Runner()
            self.assertTrue(autostart.disable(run=run, out=self.out, system="Linux"))
        self.assertEqual(run.verbs(), [f"--user disable --now {autostart.UNIT}", "--user daemon-reload"])
        self.assertFalse((self.home / ".config" / "systemd" / "user" / autostart.UNIT).exists())


class OtherSystemsTest(AutostartTestCase):
    def test_nothing_is_written_and_the_reason_is_given(self):
        run = Runner()
        self.assertFalse(autostart.enable(PYTHON, SCRIPT, run=run, out=self.out, system="Windows"))
        self.assertEqual(run.calls, [])
        self.assertIn("not supported", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
