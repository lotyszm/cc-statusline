"""Shared test scaffolding: an isolated data dir, config path and time zone."""

import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime


def local_ts(text):
    """'2026-09-30 23:30' in the test time zone as a unix timestamp."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


class IsolatedTestCase(unittest.TestCase):
    """Points the tracker at a throwaway data dir and config, in Europe/Warsaw."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc-statusline-test-")
        self._saved = {k: os.environ.get(k) for k in ("CC_STATUSLINE_DATA_DIR", "CC_STATUSLINE_CONFIG", "TZ")}
        os.environ["CC_STATUSLINE_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["CC_STATUSLINE_CONFIG"] = os.path.join(self.tmp, "config.toml")
        os.environ["TZ"] = "Europe/Warsaw"
        time.tzset()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        time.tzset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, text):
        with open(os.environ["CC_STATUSLINE_CONFIG"], "w", encoding="utf-8") as f:
            f.write(text)
