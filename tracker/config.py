"""Settings and the rules that map a directory to a client.

A missing config file means defaults. A broken one also means defaults, with
the parse error kept in `Config.error`: recording time must not stop because of
a typo, and `statusline.py --doctor` shows the error.
"""

import os
import re
from dataclasses import dataclass
from typing import Optional

from . import paths, tasks

try:
    import tomllib
except ImportError:                 # Python < 3.11
    from . import tomlite as tomllib

TEMPLATE = """\
# cc-statusline time tracking. Changes apply to past data too: reports are
# computed from the raw events every time.

idle_minutes = 15        # a gap longer than this between agent events is a break
tool_cap_minutes = 60    # one running tool (tests, a build) counts at most this long
overlap = "split"        # "split": parallel client sessions share the clock, own projects never
                         # reduce client hours; "full": every session counts fully
currency = "PLN"

# Rules map a working directory to a client; the first match wins.
# {client} takes the client name from that path segment.
# billable = true makes the agent ask about tasks in that project.
#
# [[rule]]
# path = "~/dev/clients/{client}/**"
# billable = true
#
# [[rule]]
# path = "~/dev/side-projects/**"
# client = "own"
# billable = false

# Optional hourly rates, for the amount column in reports.
# [clients.acme]
# rate = 150

# Task ids (Python regex; the first group is the id). Listing patterns here
# replaces the defaults; `ignore` adds KEY prefixes that are never tasks.
# [tasks]
# branch_patterns = ['(PROJ-\\d+)']
# prompt_patterns = ['\\b(PROJ-\\d+)\\b']
# ignore = ["CR"]
"""


@dataclass
class Rule:
    regex: re.Pattern
    client: Optional[str]
    billable: bool


def compile_path(glob):
    """'~/dev/{client}/**' -> regex. {client} is one segment, ** anything below."""
    text = os.path.expanduser(glob).rstrip("/")
    out, i = [], 0
    while i < len(text):
        if text.startswith("/**", i):
            out.append(r"(?:/.*)?")
            i += 3
        elif text.startswith("**", i):
            out.append(r".*")
            i += 2
        elif text.startswith("{client}", i):
            out.append(r"(?P<client>[^/]+)")
            i += len("{client}")
        elif text[i] == "*":
            out.append(r"[^/]*")
            i += 1
        else:
            out.append(re.escape(text[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class Config:
    def __init__(self, data=None, error=None):
        data = data or {}
        self.error = error
        self.idle = float(data.get("idle_minutes", 15)) * 60
        self.tool_cap = float(data.get("tool_cap_minutes", 60)) * 60
        self.overlap = data.get("overlap", "split")
        if self.overlap not in ("split", "full"):
            self.overlap = "split"
        self.currency = data.get("currency", "")
        self.rules = [Rule(compile_path(r["path"]), r.get("client"), bool(r.get("billable", True)))
                      for r in data.get("rule", []) if r.get("path")]
        t = data.get("tasks", {})
        self.branch_patterns = tasks._compiled(t.get("branch_patterns", tasks.BRANCH_PATTERNS))
        self.prompt_patterns = tasks._compiled(t.get("prompt_patterns", tasks.PROMPT_PATTERNS))
        self.ignore = frozenset(k.upper() for k in t.get("ignore", ()))
        self.rates = {name: c["rate"] for name, c in data.get("clients", {}).items()
                      if isinstance(c, dict) and "rate" in c}
        self._seen = {}

    def classify(self, path):
        """(client, billable) for a directory; (None, False) when no rule matches."""
        if not path:
            return (None, False)
        if path not in self._seen:
            hit = (None, False)
            for rule in self.rules:
                m = rule.regex.match(path)
                if m:
                    name = rule.client or m.groupdict().get("client")
                    hit = (name, rule.billable)
                    break
            self._seen[path] = hit
        return self._seen[path]

    def branch_task(self, branch):
        return tasks.branch_task(branch, self.branch_patterns)

    def prompt_candidates(self, text):
        return tasks.prompt_candidates(text, self.prompt_patterns, self.ignore)

    def rate(self, client):
        return self.rates.get(client)


def load(path=None):
    path = path or paths.config_path()
    try:
        with open(path, "rb") as f:
            return Config(tomllib.load(f))
    except FileNotFoundError:
        return Config()
    except Exception as e:
        return Config(error=f"{path}: {e}")
