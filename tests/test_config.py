import os
import unittest

from tracker import config
from tests.helpers import IsolatedTestCase

HOME = os.path.expanduser("~")

RULES = """
[[rule]]
path = "~/dev/agency/temp/**"
client = "agency-temp"
billable = false

[[rule]]
path = "~/dev/agency/{client}/**"

[[rule]]
path = "~/dev/ai/**"
client = "own"
billable = false
"""


class ClassifyTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(RULES)
        self.cfg = config.load()

    def test_client_is_captured_from_the_path(self):
        self.assertEqual(self.cfg.classify(f"{HOME}/dev/agency/initech/web/src"),
                         ("initech", True))

    def test_rule_matches_the_client_directory_itself(self):
        self.assertEqual(self.cfg.classify(f"{HOME}/dev/agency/initech"), ("initech", True))

    def test_first_matching_rule_wins(self):
        self.assertEqual(self.cfg.classify(f"{HOME}/dev/agency/temp/2026 - Umbrella"),
                         ("agency-temp", False))

    def test_fixed_client_name(self):
        self.assertEqual(self.cfg.classify(f"{HOME}/dev/ai/statusline"), ("own", False))

    def test_unmatched_path_has_no_client(self):
        self.assertEqual(self.cfg.classify("/tmp/elsewhere"), (None, False))
        self.assertEqual(self.cfg.classify(None), (None, False))

    def test_sibling_directory_with_common_prefix_does_not_match(self):
        self.assertEqual(self.cfg.classify(f"{HOME}/dev/ai-old/x"), (None, False))


class LoadTest(IsolatedTestCase):
    def test_defaults_without_a_config_file(self):
        cfg = config.load()
        self.assertEqual((cfg.idle, cfg.tool_cap, cfg.overlap), (900, 3600, "split"))
        self.assertEqual(cfg.classify(f"{HOME}/dev/agency/x"), (None, False))
        self.assertIsNone(cfg.error)

    def test_minutes_and_overlap_are_read(self):
        self.write_config('idle_minutes = 10\ntool_cap_minutes = 30\noverlap = "full"\n')
        cfg = config.load()
        self.assertEqual((cfg.idle, cfg.tool_cap, cfg.overlap), (600, 1800, "full"))

    def test_rates_per_client(self):
        self.write_config('currency = "PLN"\n[clients.initech]\nrate = 150\n')
        cfg = config.load()
        self.assertEqual(cfg.rate("initech"), 150)
        self.assertIsNone(cfg.rate("Globex"))
        self.assertEqual(cfg.currency, "PLN")

    def test_custom_branch_patterns_replace_the_defaults(self):
        self.write_config("[tasks]\nbranch_patterns = ['(PROJ-\\d+)']\n")
        cfg = config.load()
        self.assertEqual(cfg.branch_task("feature/PROJ-7-x"), "PROJ-7")
        self.assertIsNone(cfg.branch_task("feature/12345/x"))

    def test_ignore_extends_the_default_list(self):
        self.write_config('[tasks]\nignore = ["CR"]\n')
        cfg = config.load()
        self.assertEqual(cfg.prompt_candidates("CR-14, UTF-8 i SHOP-1"), ["SHOP-1"])

    def test_broken_file_falls_back_to_defaults_and_reports_it(self):
        self.write_config("idle_minutes = = 3\n")
        cfg = config.load()
        self.assertEqual(cfg.idle, 900)
        self.assertIn("config.toml", cfg.error)


if __name__ == "__main__":
    unittest.main()
