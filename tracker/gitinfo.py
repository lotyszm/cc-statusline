"""Repository root and branch, read from .git without spawning git.

Hooks run on every tool call, so starting a git process each time would cost
more than everything else the hook does.
"""

from pathlib import Path

MAX_DEPTH = 40


def repo_info(path):
    """(repo root, branch) for a directory inside a repository, else (None, None).

    A detached HEAD is reported as its short sha, like git does in prompts.
    """
    if not path:
        return (None, None)
    p = Path(path)
    for _ in range(MAX_DEPTH):
        dotgit = p / ".git"
        gitdir = None
        if dotgit.is_dir():
            gitdir = dotgit
        elif dotgit.is_file():                      # worktree or submodule pointer
            try:
                text = dotgit.read_text(encoding="utf-8").strip()
            except OSError:
                return (None, None)
            if text.startswith("gitdir:"):
                target = Path(text.split(":", 1)[1].strip())
                gitdir = target if target.is_absolute() else (p / target)
        if gitdir is not None:
            try:
                head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
            except OSError:
                return (str(p), None)
            branch = head.split("refs/heads/", 1)[1] if "refs/heads/" in head else head[:7]
            return (str(p), branch)
        if p.parent == p:
            break
        p = p.parent
    return (None, None)
