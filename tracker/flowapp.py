"""Export of tracked time to FlowApp: proposed entries, their review state, the API.

The tracker's intervals become blocks - one FlowApp entry each: a task's time
on one local day, overlaps joined, short breaks glued, each block rounded up
to the configured step. Blocks a person edited, skipped or sent live in the
`flowapp_exports` table; the rest is recomputed on every request. Once a block
is sent, its stored values are what the page shows: no moment of tracked time
may reach FlowApp twice.

The token never leaves this module's callers' process: it is not written to
the config, the database, a log or any response of the dashboard.
"""

import math
import os
import stat
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import db, ledger, paths

TOKEN_ENV = "FLOWAPP_TOKEN"
KEYCHAIN_SERVICE = "flowapp-api-token"


def load_token(env=None, platform=None, run=None):
    """(token, None) or (None, problem): the FlowApp API token and where it failed.

    Looked up in FLOWAPP_TOKEN, then in the token file - only when nobody but
    the owner can read it, otherwise the problem is "token_file_mode" and the
    file stays unread - then, on macOS, in the Keychain. A login service does
    not see the shell's variables, hence the file and the Keychain.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    run = subprocess.run if run is None else run
    token = (env.get(TOKEN_ENV) or "").strip()
    if token:
        return token, None
    path = paths.flowapp_token_path()
    try:
        mode = path.stat().st_mode
    except OSError:
        mode = None
    if mode is not None:
        if stat.S_IMODE(mode) & 0o077:
            return None, "token_file_mode"
        try:
            token = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            token = ""
        if token:
            return token, None
    if platform == "darwin":
        try:
            r = run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            r = None
        token = (r.stdout or "").strip() if r is not None and r.returncode == 0 else ""
        if token:
            return token, None
    return None, "no_token"


# --- blocks -------------------------------------------------------------------

@dataclass
class Block:
    """One proposed FlowApp entry: a task's (or a project's) time on one local day.

    `tracked_start`/`tracked_stop` span the glued work, `start`/`stop` are what
    is sent: the start cut to a whole second, the stop rounded up to the step.
    `active_seconds` is the work itself, without the glued breaks.
    """
    key: str
    client: Optional[str]
    task: Optional[str]
    project: Optional[str]
    day: str
    tracked_start: float
    tracked_stop: float
    start: float
    stop: float
    active_seconds: float
    comment: str
    redmine_id: Optional[str]

    @property
    def external_id(self):
        return f"cc:{self.key}:{int(self.tracked_start)}"


def block_key(k):
    """What a block belongs to: the task, or "<client>/<project folder>" without one."""
    if k.task:
        return k.task
    return f"{k.client or '-'}/{Path(k.project).name if k.project else '-'}"


def _day_of(ts):
    return datetime.fromtimestamp(ts).date()


def _midnight(d):
    return datetime.combine(d, datetime.min.time()).timestamp()


def _split_days(s, e):
    """[(day, start, end)] of [s, e) cut at local midnights."""
    out = []
    while s < e:
        d = _day_of(s)
        cut = min(e, _midnight(d + timedelta(days=1)))
        out.append((d, s, cut))
        s = cut
    return out


def _glue(spans, gap_s):
    """Sorted spans joined when they overlap or the break is at most gap_s.

    [(start, end, active seconds)]: active counts the spans, not the breaks.
    """
    out = []
    for s, e in sorted(spans):
        if out and s - out[-1][1] <= gap_s:
            last = out[-1]
            covered = max(0.0, min(e, last[1]) - s)        # the part of this span already counted
            out[-1] = (last[0], max(last[1], e), last[2] + (e - s) - covered)
        else:
            out.append((s, e, e - s))
    return out


def _round(start, stop, step_s):
    return start + math.ceil((stop - start) / step_s) * step_s if stop > start else start + step_s


def build_blocks(intervals, *, gap_s, step_s, titles, tickets, billable_only=True):
    """Blocks from labelled intervals [(start, end, ledger.Key)]. Pure: no database.

    Per local day and task: overlapping time (several sessions, subagents) is
    joined, breaks up to gap_s glued, each block rounded up to step_s; a block
    whose rounded stop reaches the next one is merged with it and rounded
    again. No block crosses midnight. The order is by day, then start.
    """
    groups = defaultdict(list)          # (day, client, key) -> [(start, end)]
    meta = {}
    for s, e, k in intervals:
        if billable_only and not k.billable:
            continue
        bkey = block_key(k)
        for d, p0, p1 in _split_days(s, e):
            groups[(d, k.client, bkey)].append((p0, p1))
            meta.setdefault((d, k.client, bkey), k)
    out = []
    for (d, client, bkey), spans in groups.items():
        k = meta[(d, client, bkey)]
        day_end = _midnight(d + timedelta(days=1))
        glued = _glue(spans, gap_s)
        merged = []                     # [tracked_start, tracked_stop, active, start, stop]
        for s, e, active in glued:
            start = float(math.floor(s))
            if merged and merged[-1][4] >= s:
                last = merged[-1]
                last[1], last[2] = max(last[1], e), last[2] + active
                last[4] = _round(last[3], last[1], step_s)
            else:
                merged.append([s, e, active, start, _round(start, e, step_s)])
        title = titles.get(bkey) if k.task else None
        ticket = str(tickets.get(bkey) or "").strip() if k.task else ""
        for ts, te, active, start, stop in merged:
            out.append(Block(key=bkey, client=client, task=k.task, project=k.project, day=d.isoformat(),
                             tracked_start=ts, tracked_stop=te, start=start, stop=min(stop, day_end),
                             active_seconds=active, comment=title or bkey,
                             redmine_id=ticket if ticket.isdigit() else None))
    out.sort(key=lambda b: (b.day, b.start, b.key))
    return out


def collect_blocks(conn, cfg, day_from, day_to, billable_only=True):
    """Blocks of the days day_from..day_to (dates) from the tracker database.

    Always counts every agent apart (per_agent=True, the "full" view), however
    the reports share the clock; whether subagents are workers of their own
    follows the user's `subagents` setting.
    """
    settings = cfg.flowapp
    step = (settings.round_minutes if settings else 5) * 60
    gap = (settings.gap_minutes if settings else 15) * 60
    start, end = _midnight(day_from), _midnight(day_to + timedelta(days=1))
    intervals = ledger.intervals(conn, cfg, start, end, per_agent=True)
    titles, tickets = {}, {}
    for task, row in db.task_details(conn).items():
        titles[task] = row["title"]
        tickets[task] = row["ticket"]
    return build_blocks(intervals, gap_s=gap, step_s=step, titles=titles, tickets=tickets,
                        billable_only=billable_only)
