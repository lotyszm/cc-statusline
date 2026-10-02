"""Finds task ids in branch names and in what you type.

Branch names are deliberate, so a match there is trusted. A match in a prompt
is only a candidate: the agent asks before anything is recorded, which is why
the prompt patterns can afford to be a little generous.
"""

import re

BRANCH_PATTERNS = (
    r"\b([A-Z]{2,}[A-Z0-9]*-\d+)",              # feat/PROJ-123-checkout
    r"(?:^|/)(\d{4,6})(?=$|[/_-])",             # feature/12345/hotfix; 8-digit dates fail
)

PROMPT_PATTERNS = (
    # Tracker keys. Two letters minimum, so spec references like F3-40 stay out.
    r"\b([A-Z]{2,}[A-Z0-9]*-\d{1,6})\b",
    # A number right after a word that names a task, in Polish or English.
    r"(?i)\b(?:zadani\w*|task\w*|ticket\w*|tiket\w*|issue\w*|story|bug|redmine)"
    r"\s*(?:nr\.?|no\.?|numer)?\s*[:#]?\s*#?(\d{3,7})\b",
    r"/issues/(\d+)\b",                         # Redmine, GitHub, GitLab links
    r"/browse/([A-Z][A-Z0-9]+-\d+)\b",          # Jira links
)

# Prefixes of KEY-123 tokens that are standards, versions or units, not tasks.
IGNORED_KEYS = frozenset("""
    AES API CET CEST COVID CSS CVE CWE EAN ECMA EN ES EU GMT GPT HTML HTTP HTTPS
    ID IE IOS ISBN ISO JSON MD NIP OS OWASP PDF PEP PHP PL PR RFC RSA SEO SHA
    SSL TLS TOP US USB UTC UTF UUID VAT WCAG XML
""".split())

MAX_CANDIDATES = 5

_CODE_BLOCK = re.compile(r"```.*?(?:```|\Z)", re.S)


def _compiled(patterns):
    return [p if isinstance(p, re.Pattern) else re.compile(p) for p in patterns]


_BRANCH_RE = _compiled(BRANCH_PATTERNS)
_PROMPT_RE = _compiled(PROMPT_PATTERNS)


def _hit(m):
    return m.group(1) if m.re.groups else m.group(0)


def branch_task(branch, patterns=None):
    """The task a branch name points at, or None."""
    if not branch:
        return None
    for rx in _compiled(patterns) if patterns is not None else _BRANCH_RE:
        m = rx.search(branch)
        if m:
            return _hit(m)
    return None


def prompt_candidates(text, patterns=None, ignore=frozenset()):
    """Task ids mentioned in text, in order of appearance, each once."""
    if not text:
        return []
    text = _CODE_BLOCK.sub(" ", text)        # pasted logs and code are not requests
    skip = IGNORED_KEYS | ignore
    found = []
    for rx in _compiled(patterns) if patterns is not None else _PROMPT_RE:
        for m in rx.finditer(text):
            cand = _hit(m)
            if "-" in cand and cand.split("-", 1)[0].upper() in skip:
                continue
            found.append((m.start(), cand))
    out = []
    for _, cand in sorted(found):
        if cand not in out:
            out.append(cand)
    return out[:MAX_CANDIDATES]
