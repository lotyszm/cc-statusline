import os
import subprocess

from tracker import config, flowapp, paths
from tests.helpers import IsolatedTestCase


class FakeRun:
    """Stands in for subprocess.run: records calls, answers like `security`."""

    def __init__(self, stdout="", returncode=0, error=None):
        self.calls = []
        self.stdout, self.returncode, self.error = stdout, returncode, error

    def __call__(self, args, **kw):
        self.calls.append(args)
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr="")


class SettingsTest(IsolatedTestCase):
    def test_no_section_means_off(self):
        self.assertIsNone(config.load().flowapp)

    def test_section_without_url_means_off(self):
        self.write_config("[flowapp]\nround_minutes = 10\n")
        self.assertIsNone(config.load().flowapp)

    def test_defaults(self):
        self.write_config('[flowapp]\nurl = "https://example.test/api/v1/"\n')
        s = config.load().flowapp
        self.assertEqual((s.url, s.round_minutes, s.gap_minutes), ("https://example.test/api/v1", 5, 15))

    def test_allowed_steps_and_fallback(self):
        for given, expected in ((10, 10), (15, 15), (7, 5), ("x", 5)):
            with self.subTest(given=given):
                value = f'"{given}"' if isinstance(given, str) else given
                self.write_config(f'[flowapp]\nurl = "https://e.test"\nround_minutes = {value}\ngap_minutes = 20\n')
                s = config.load().flowapp
                self.assertEqual((s.round_minutes, s.gap_minutes), (expected, 20))

    def test_negative_gap_falls_back(self):
        self.write_config('[flowapp]\nurl = "https://e.test"\ngap_minutes = -3\n')
        self.assertEqual(config.load().flowapp.gap_minutes, 15)


class TokenTest(IsolatedTestCase):
    def write_token(self, text, mode):
        path = paths.flowapp_token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.chmod(path, mode)
        return path

    def test_token_file_sits_next_to_the_config(self):
        self.assertEqual(paths.flowapp_token_path(), paths.config_path().parent / "flowapp-token")

    def test_env_wins_over_file(self):
        self.write_token("flw_file\n", 0o600)
        run = FakeRun("flw_keychain\n")
        self.assertEqual(flowapp.load_token(env={"FLOWAPP_TOKEN": " flw_env "}, platform="darwin", run=run),
                         ("flw_env", None))
        self.assertEqual(run.calls, [])

    def test_private_file_is_read_and_stripped(self):
        self.write_token("  flw_file\n", 0o600)
        self.assertEqual(flowapp.load_token(env={}, platform="linux", run=FakeRun()), ("flw_file", None))

    def test_readable_file_is_refused_unread(self):
        self.write_token("flw_file\n", 0o644)
        run = FakeRun("flw_keychain\n")
        self.assertEqual(flowapp.load_token(env={}, platform="darwin", run=run), (None, "token_file_mode"))
        self.assertEqual(run.calls, [])

    def test_keychain_only_on_macos(self):
        run = FakeRun("flw_keychain\n")
        self.assertEqual(flowapp.load_token(env={}, platform="darwin", run=run), ("flw_keychain", None))
        self.assertEqual(run.calls[0][:2], ["security", "find-generic-password"])
        self.assertIn("flowapp-api-token", run.calls[0])
        linux = FakeRun("flw_keychain\n")
        self.assertEqual(flowapp.load_token(env={}, platform="linux", run=linux), (None, "no_token"))
        self.assertEqual(linux.calls, [])

    def test_keychain_miss_or_failure_is_no_token(self):
        for run in (FakeRun("", returncode=44), FakeRun(error=FileNotFoundError("security")),
                    FakeRun(error=subprocess.TimeoutExpired("security", 5))):
            with self.subTest(run=run):
                self.assertEqual(flowapp.load_token(env={}, platform="darwin", run=run), (None, "no_token"))

    def test_nothing_anywhere(self):
        self.assertEqual(flowapp.load_token(env={}, platform="linux", run=FakeRun()), (None, "no_token"))

    def test_empty_env_and_file_are_skipped(self):
        self.write_token("\n", 0o600)
        self.assertEqual(flowapp.load_token(env={"FLOWAPP_TOKEN": ""}, platform="linux", run=FakeRun()),
                         (None, "no_token"))
