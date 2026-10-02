import unittest

from tracker.tasks import branch_task, prompt_candidates


class BranchTaskTest(unittest.TestCase):
    def test_numeric_segment_is_a_task(self):
        self.assertEqual(branch_task("feature/12345/hotfix-pricing"), "12345")
        self.assertEqual(branch_task("feature/23456/handle-user_errors"), "23456")
        self.assertEqual(branch_task("feature/34567"), "34567")

    def test_dates_in_branch_names_are_not_tasks(self):
        self.assertIsNone(branch_task("fix/ahrefs/20260922"))
        self.assertIsNone(branch_task("fix/20260916"))

    def test_tracker_key_is_a_task(self):
        self.assertEqual(branch_task("feat/SHOP-42-checkout"), "SHOP-42")

    def test_plain_branches_have_no_task(self):
        for name in ("main", "HEAD", "feat/product-card", "optimization_2", "", None):
            self.assertIsNone(branch_task(name), name)


class PromptCandidatesTest(unittest.TestCase):
    def test_tracker_keys_need_two_letters(self):
        self.assertEqual(prompt_candidates("popraw SHOP-42 zgodnie z F3-40 i R6-14"), ["SHOP-42"])

    def test_standards_and_versions_are_ignored(self):
        self.assertEqual(prompt_candidates("UTF-8, SHA-256, ISO-8601, GPT-4, TOP-10"), [])

    def test_number_after_a_task_word(self):
        self.assertEqual(prompt_candidates("to jest zadanie 12345"), ["12345"])
        self.assertEqual(prompt_candidates("task #482 oraz issue 418"), ["482", "418"])

    def test_tracker_links(self):
        self.assertEqual(prompt_candidates("zob. https://redmine.example.com/issues/23456"), ["23456"])
        self.assertEqual(prompt_candidates("https://x.atlassian.net/browse/ABC-12"), ["ABC-12"])

    def test_bare_numbers_and_colours_are_not_tasks(self):
        self.assertEqual(prompt_candidates("kolor #333333, port 3000, 12345 sztuk"), [])

    def test_code_blocks_are_skipped(self):
        self.assertEqual(prompt_candidates("log:\n```\nERR-500 at line\n```\nnapraw"), [])

    def test_each_candidate_once_in_order(self):
        self.assertEqual(prompt_candidates("ABC-2, potem SHOP-42 i znowu ABC-2"), ["ABC-2", "SHOP-42"])

    def test_extra_ignored_keys(self):
        self.assertEqual(prompt_candidates("CR-14 i SHOP-1", ignore=frozenset({"CR"})), ["SHOP-1"])


if __name__ == "__main__":
    unittest.main()
