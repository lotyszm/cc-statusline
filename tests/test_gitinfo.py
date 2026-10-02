import os
import tempfile
import unittest

from tracker.gitinfo import repo_info


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class RepoInfoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cc-statusline-git-"))

    def test_branch_and_root_from_a_nested_directory(self):
        root = os.path.join(self.tmp, "shop")
        write(os.path.join(root, ".git", "HEAD"), "ref: refs/heads/feature/12345/hotfix\n")
        os.makedirs(os.path.join(root, "src", "app"))
        self.assertEqual(repo_info(os.path.join(root, "src", "app")), (root, "feature/12345/hotfix"))

    def test_detached_head_shows_a_short_sha(self):
        root = os.path.join(self.tmp, "shop")
        write(os.path.join(root, ".git", "HEAD"), "8fa8248e303219400c646a885e36dfc52eae33d8\n")
        self.assertEqual(repo_info(root), (root, "8fa8248"))

    def test_worktree_pointer_file(self):
        gitdir = os.path.join(self.tmp, "main", ".git", "worktrees", "wt")
        write(os.path.join(gitdir, "HEAD"), "ref: refs/heads/feat/checkout\n")
        wt = os.path.join(self.tmp, "wt")
        write(os.path.join(wt, ".git"), f"gitdir: {gitdir}\n")
        self.assertEqual(repo_info(wt), (wt, "feat/checkout"))

    def test_outside_a_repository(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(repo_info(plain), (None, None))
        self.assertEqual(repo_info(None), (None, None))


if __name__ == "__main__":
    unittest.main()
