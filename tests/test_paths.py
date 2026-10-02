import os
import unittest
from pathlib import Path

from tracker import paths

VARS = ("CC_STATUSLINE_DATA_DIR", "CC_STATUSLINE_CONFIG", "XDG_DATA_HOME", "XDG_CONFIG_HOME")


class PathsTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in VARS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_xdg_locations_under_the_cc_statusline_name(self):
        os.environ["XDG_DATA_HOME"] = "/xdg/data"
        os.environ["XDG_CONFIG_HOME"] = "/xdg/config"
        self.assertEqual(paths.data_dir(), Path("/xdg/data/cc-statusline"))
        self.assertEqual(paths.db_path(), Path("/xdg/data/cc-statusline/tracker.db"))
        self.assertEqual(paths.status_dir(), Path("/xdg/data/cc-statusline/status"))
        self.assertEqual(paths.log_path(), Path("/xdg/data/cc-statusline/hook.log"))
        self.assertEqual(paths.config_path(), Path("/xdg/config/cc-statusline/config.toml"))

    def test_home_directories_without_xdg(self):
        self.assertEqual(paths.data_dir(), Path.home() / ".local" / "share" / "cc-statusline")
        self.assertEqual(paths.config_path(), Path.home() / ".config" / "cc-statusline" / "config.toml")

    def test_environment_overrides_win(self):
        os.environ["XDG_DATA_HOME"] = "/xdg/data"
        os.environ["CC_STATUSLINE_DATA_DIR"] = "/elsewhere/data"
        os.environ["CC_STATUSLINE_CONFIG"] = "/elsewhere/tracker.toml"
        self.assertEqual(paths.db_path(), Path("/elsewhere/data/tracker.db"))
        self.assertEqual(paths.config_path(), Path("/elsewhere/tracker.toml"))


if __name__ == "__main__":
    unittest.main()
