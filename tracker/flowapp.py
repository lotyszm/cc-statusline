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

import json
import math
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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


# --- export state ---------------------------------------------------------------

MAX_AGE_S = 62 * 86400          # FlowApp takes entries at most 62 days back
MAX_ENTRY_S = 24 * 3600         # and at most 24 hours long
STATUSES = ("new", "draft", "sent", "changed", "skipped", "blocked", "error", "today", "deleted_remote")
FLAGS = ("tracker_changed", "task_changed", "changed_remote", "exported", "overlaps_sent")
ACTIONS = ("edit", "skip", "unskip", "merge_next", "resend")


def load_exports(conn, day_from, day_to):
    """{external_id: row} of the stored blocks of the days day_from..day_to (dates or ISO strings)."""
    lo, hi = str(day_from), str(day_to)
    return {r["external_id"]: r for r in conn.execute(
        "SELECT * FROM flowapp_exports WHERE day >= ? AND day <= ? ORDER BY day, orig_start", (lo, hi))}


def block_from_row(row):
    """The block a stored row stands for, with the values it is to send."""
    return Block(key=row["task"], client=row["client"], task=None if "/" in row["task"] else row["task"],
                 project=None, day=row["day"],
                 tracked_start=row["tracked_start"] if row["tracked_start"] is not None else row["orig_start"],
                 tracked_stop=row["tracked_stop"] if row["tracked_stop"] is not None else row["orig_stop"],
                 start=row["start"], stop=row["stop"], active_seconds=row["active_seconds"] or 0.0,
                 comment=row["comment"], redmine_id=row["redmine_id"])


def _insert(conn, block, status, now, merged_into=None):
    conn.execute(
        "INSERT INTO flowapp_exports (external_id, task, client, day, orig_start, orig_stop, tracked_start, "
        "tracked_stop, active_seconds, start, stop, comment, redmine_id, status, merged_into, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (block.external_id, block.key, block.client, block.day, block.start, block.stop, block.tracked_start,
         block.tracked_stop, block.active_seconds, block.start, block.stop, block.comment, block.redmine_id,
         status, merged_into, now))
    return _row(conn, block.external_id)


def _row(conn, external_id):
    return conn.execute("SELECT * FROM flowapp_exports WHERE external_id = ?", (external_id,)).fetchone()


def _update(conn, external_id, now, **fields):
    names = list(fields)
    conn.execute(f"UPDATE flowapp_exports SET {''.join(f'{n} = ?, ' for n in names)}updated_at = ? "
                 f"WHERE external_id = ?", [fields[n] for n in names] + [now, external_id])


def _clean_redmine(value):
    text = str(value if value is not None else "").strip().lstrip("#")
    if text and not text.isdigit():
        raise ValueError("Redmine ID must be a number")
    return text or None


def _same_sent(values, row):
    """Whether the values to send are what FlowApp already has."""
    return (int(values["start"]) == int(row["sent_start"] or 0) and int(values["stop"]) == int(row["sent_stop"] or 0)
            and (values["comment"] or "") == (row["sent_comment"] or "")
            and (values["redmine_id"] or None) == (row["sent_redmine_id"] or None))


def save_draft(conn, block, action, fields, now):
    """Store a person's change to one block. ValueError for a change that cannot be made.

    `block` is the proposal's block of that external_id, or None when only the
    stored row is left (it is then looked up by fields["external_id"]).
    Actions: edit (start, stop, comment, redmine_id, export_comment), skip,
    unskip, merge_next (fields["next"]: the following block of the task that
    day), resend (a sent entry deleted in FlowApp: forget it was sent).
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    external_id = block.external_id if block is not None else fields.get("external_id")
    row = _row(conn, external_id) if external_id else None
    if row is None:
        if block is None:
            raise LookupError(f"no block {external_id}")
        if action in ("unskip", "resend"):
            raise ValueError(f"{action}: the block is not stored")
        row = _insert(conn, block, "draft", now)
    if row["status"] == "merged":
        raise ValueError("the block was merged into another one")
    sent = row["flowapp_id"] is not None

    if action == "edit":
        values = {"start": row["start"], "stop": row["stop"], "comment": row["comment"],
                  "redmine_id": row["redmine_id"]}
        for name in ("start", "stop"):
            if fields.get(name) is not None:
                values[name] = float(fields[name])
        if "comment" in fields and fields["comment"] is not None:
            values["comment"] = str(fields["comment"]).strip()
        if "redmine_id" in fields:
            values["redmine_id"] = _clean_redmine(fields["redmine_id"])
        if values["stop"] <= values["start"]:
            raise ValueError("the end must be after the start")
        if values["stop"] - values["start"] > MAX_ENTRY_S:
            raise ValueError("an entry can be at most 24 hours long")
        if _day_of(values["start"]).isoformat() != row["day"]:
            raise ValueError(f"the start must be on {row['day']}")
        if not values["comment"]:
            raise ValueError("the comment must not be empty")
        extra = {}
        if "export_comment" in fields:
            extra["export_comment"] = str(fields["export_comment"] or "").strip() or None
        status = ("sent" if _same_sent(values, row) else "changed") if sent else "draft"
        _update(conn, row["external_id"], now, status=status, error=None, **values, **extra)
    elif action == "skip":
        if sent:
            raise ValueError("an entry already sent cannot be skipped; delete it in FlowApp")
        _update(conn, row["external_id"], now, status="skipped")
    elif action == "unskip":
        if row["status"] != "skipped":
            raise ValueError("the block is not skipped")
        _update(conn, row["external_id"], now, status="draft")
    elif action == "merge_next":
        nxt = fields.get("next")
        if nxt is None:
            raise ValueError("there is no next block to merge with")
        nrow = _row(conn, nxt.external_id)
        if sent or (nrow is not None and nrow["flowapp_id"] is not None):
            raise ValueError("entries already sent cannot be merged")
        if nxt.day != row["day"] or nxt.key != row["task"] or nxt.external_id == row["external_id"]:
            raise ValueError("only the next block of the same task and day can be merged")
        stop = max(row["stop"], nxt.stop)
        if stop - row["start"] > MAX_ENTRY_S:
            raise ValueError("an entry can be at most 24 hours long")
        _update(conn, row["external_id"], now, status="draft", stop=stop,
                tracked_stop=max(row["tracked_stop"] or row["orig_stop"], nxt.tracked_stop),
                active_seconds=(row["active_seconds"] or 0.0) + nxt.active_seconds)
        if nrow is None:
            _insert(conn, nxt, "merged", now, merged_into=row["external_id"])
        else:
            _update(conn, nxt.external_id, now, status="merged", merged_into=row["external_id"])
    else:                                   # resend
        if not sent:
            raise ValueError("the block was never sent")
        unedited = block is not None and (int(row["start"]), int(row["stop"]), row["comment"], row["redmine_id"]) \
            == (int(block.start), int(block.stop), block.comment, block.redmine_id)
        if unedited:
            conn.execute("DELETE FROM flowapp_exports WHERE external_id = ?", (row["external_id"],))
        else:
            _update(conn, row["external_id"], now, status="draft", flowapp_id=None, sent_start=None,
                    sent_stop=None, sent_comment=None, sent_redmine_id=None, sent_at=None, error=None,
                    export_comment=None)


def parse_time(text):
    """Epoch seconds of an ISO 8601 time from FlowApp ("...Z" or with an offset)."""
    if not text:
        return None
    text = str(text).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def iso(ts):
    """Local ISO 8601 with offset, whole seconds: what FlowApp is sent."""
    return datetime.fromtimestamp(math.floor(ts)).astimezone().isoformat(timespec="seconds")


def mark_sent(conn, external_id, entry, now):
    """Store FlowApp's answer for a block: its id and what it now holds.

    The values come from the answer, not from the request: a replayed create
    returns the entry as it was first made, which may differ from this
    request; the row then stays `changed`.
    """
    row = _row(conn, external_id)
    # A field the answer leaves out was accepted as sent.
    sent = {"sent_start": parse_time(entry["startedAt"]) if entry.get("startedAt") else row["start"],
            "sent_stop": parse_time(entry["stoppedAt"]) if entry.get("stoppedAt") else row["stop"],
            "sent_comment": entry["comment"] if "comment" in entry else row["comment"],
            "sent_redmine_id": (str(entry["redmineId"]) if entry["redmineId"] not in (None, "") else None)
            if "redmineId" in entry else row["redmine_id"]}
    values = {"start": row["start"], "stop": row["stop"], "comment": row["comment"], "redmine_id": row["redmine_id"]}
    status = "sent" if _same_sent(values, sent) else "changed"
    _update(conn, external_id, now, flowapp_id=str(entry.get("id")), status=status, error=None, sent_at=now,
            export_comment=None, **sent)


def mark_error(conn, external_id, code, message, now):
    _update(conn, external_id, now, status="error", error=f"{code}: {message}" if code else message)


def _overlaps(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def _covered(s, e, spans):
    """Whether [s, e) lies within the union of spans."""
    for p0, p1 in _union_spans(spans):
        if p1 <= s:
            continue
        if p0 > s:
            return False
        s = p1
        if s >= e:
            return True
    return s >= e


def _union_spans(spans):
    out = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _item(external_id, key, client, day, start, stop, tracked_start, tracked_stop, active, comment, redmine_id,
          status, flags=(), error=None, flowapp_id=None, export_status=None):
    return {"external_id": external_id, "key": key, "client": client, "day": day, "start": start, "stop": stop,
            "tracked_start": tracked_start, "tracked_stop": tracked_stop, "active_seconds": active,
            "comment": comment, "redmine_id": redmine_id, "status": status, "flags": list(flags),
            "error": error, "flowapp_id": flowapp_id, "export_status": export_status}


def reconcile(blocks, exports, remote, today, now):
    """The proposal merged with what was stored and what FlowApp holds: one dict per entry.

    Stored rows show their stored values; a proposal block is matched to a row
    by external_id, else by overlapping time (a sent row of the same task, a
    sent row of another task that lost that time to this one, or an unsent row
    of the same task), and is then not offered again. A proposal block that
    only runs alongside a sent entry of another task is new, flagged
    overlaps_sent. `remote` is FlowApp's entries for the days, or None when
    FlowApp could not be asked: then nothing is said about the remote side.
    Times stay epochs; ordered by day, start, key.
    """
    today = str(today)
    rows = [r for r in exports.values()]
    live = [r for r in rows if r["status"] != "merged"]
    sent_rows = [r for r in live if r["flowapp_id"] is not None]
    unsent_rows = sorted((r for r in rows if r["flowapp_id"] is None), key=lambda r: (r["orig_start"], r["external_id"]))
    proposal = defaultdict(list)            # (day, key) -> [(start, stop)]
    for b in blocks:
        proposal[(b.day, b.key)].append((b.start, b.stop))

    claimed = defaultdict(list)             # external_id -> [Block]
    moved = set()                           # sent rows whose time the tracker gave another task
    unclaimed = []
    for b in blocks:
        if b.external_id in exports:
            claimed[b.external_id].append(b)
            continue
        hits = []
        for r in sent_rows:
            if r["day"] != b.day or not _overlaps(b.start, b.stop, r["sent_start"], r["sent_stop"]):
                continue
            if r["task"] == b.key:
                hits.append(r)
                continue
            o0, o1 = max(b.start, r["sent_start"]), min(b.stop, r["sent_stop"])
            if not _covered(o0, o1, proposal[(r["day"], r["task"])]):
                hits.append(r)
                moved.add(r["external_id"])
        if not hits:
            hits = [r for r in unsent_rows if r["day"] == b.day and r["task"] == b.key
                    and _overlaps(b.start, b.stop, r["orig_start"], r["orig_stop"])]
        for r in hits:
            claimed[r["external_id"]].append(b)
        if not hits:
            unclaimed.append(b)

    remote_by_id, remote_by_ext = {}, {}
    for e in remote or ():
        ext = e.get("externalId") or ""
        if not ext.startswith("cc:"):
            continue                        # entries made by hand or by another tool
        remote_by_id[str(e.get("id"))] = e
        remote_by_ext[ext] = e

    oldest = now - MAX_AGE_S

    def day_status(day, status):
        if status in ("new", "draft", "changed", "error") and _midnight(date.fromisoformat(day)) < oldest:
            return "blocked"
        if status == "new" and day == today:
            return "today"
        return status

    out = []
    for r in live:
        flags = []
        mine = claimed.get(r["external_id"], [])
        if mine:
            ts, te = min(b.tracked_start for b in mine), max(b.tracked_stop for b in mine)
            active = sum(b.active_seconds for b in mine)
        else:
            ts = te = r["tracked_start"] if r["tracked_start"] is not None else r["orig_start"]
            active = 0.0
        if (int(ts), int(te)) != (int(r["tracked_start"] or r["orig_start"]), int(r["tracked_stop"] or r["orig_stop"])):
            flags.append("tracker_changed")
        if r["external_id"] in moved:
            if "tracker_changed" not in flags:
                flags.append("tracker_changed")
            flags.append("task_changed")
        status = r["status"]
        export_status = None
        if r["flowapp_id"] is not None and remote is not None:
            e = remote_by_id.get(str(r["flowapp_id"])) or remote_by_ext.get(r["external_id"])
            if e is None:
                status = "deleted_remote"
            else:
                export_status = e.get("exportStatus")
                if export_status not in (None, "none"):
                    flags.append("exported")
                there = (int(parse_time(e.get("startedAt")) or 0), int(parse_time(e.get("stoppedAt")) or 0),
                         e.get("comment") or "", str(e.get("redmineId") or ""))
                ours = (int(r["sent_start"] or 0), int(r["sent_stop"] or 0), r["sent_comment"] or "",
                        r["sent_redmine_id"] or "")
                if there != ours:
                    flags.append("changed_remote")
        out.append(_item(r["external_id"], r["task"], r["client"], r["day"], r["start"], r["stop"], ts, te, active,
                         r["comment"], r["redmine_id"], day_status(r["day"], status), flags, r["error"],
                         r["flowapp_id"], export_status))

    for b in unclaimed:
        out.append(_item(b.external_id, b.key, b.client, b.day, b.start, b.stop, b.tracked_start, b.tracked_stop,
                         b.active_seconds, b.comment, b.redmine_id, day_status(b.day, "new")))

    sent_spans = defaultdict(list)
    for r in sent_rows:
        sent_spans[r["day"]].append((r["external_id"], r["sent_start"], r["sent_stop"]))
    for it in out:
        if it["flowapp_id"] is None and any(x != it["external_id"] and _overlaps(it["start"], it["stop"], s, e)
                                            for x, s, e in sent_spans[it["day"]]):
            it["flags"].append("overlaps_sent")
    out.sort(key=lambda it: (it["day"], it["start"], it["key"]))
    return out


# --- FlowApp API ------------------------------------------------------------------

ENTRIES_PATH = "/tracker/entries"
RANGE_PARAMS = ("from", "to")           # GET /tracker/entries?from=YYYY-MM-DD&to=YYYY-MM-DD
MAX_RANGE_DAYS = 93                     # FlowApp's longest range per list request


class ApiError(Exception):
    """A FlowApp answer other than success, or no answer (status 0, code "unreachable")."""

    def __init__(self, status, code, message, field=None, retry_after=None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.field, self.retry_after = status, code, message, field, retry_after


def _unwrap(data):
    """An entry or a list from a body that may wrap it in {"data": ...}."""
    if isinstance(data, dict) and "id" not in data:
        for name in ("data", "entries", "items"):
            if name in data:
                return data[name]
    return data


class Client:
    """FlowApp API v1 over urllib. The token goes only into the Authorization header."""

    def __init__(self, url, token, timeout=15):
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def __repr__(self):
        return f"Client({self.url!r})"

    def _call(self, method, path, body=None, query=None):
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, r.headers, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                raise self._error(e) from None
            finally:
                e.close()
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(0, "unreachable", str(getattr(e, "reason", e))) from None

    @staticmethod
    def _error(e):
        try:
            err = json.loads(e.read() or b"{}").get("error") or {}
        except (ValueError, AttributeError, OSError):
            err = {}
        if not isinstance(err, dict):
            err = {"message": str(err)}
        retry = None
        try:
            retry = float(e.headers.get("Retry-After")) if e.headers and e.headers.get("Retry-After") else None
        except ValueError:
            pass
        return ApiError(e.code, err.get("code") or f"http_{e.code}", err.get("message") or e.reason or "",
                        err.get("field"), retry)

    def me(self):
        return self._call("GET", "/me")[2] or {}

    def entries(self, day_from, day_to):
        """Every entry of the days day_from..day_to, asked for at most 93 days at a time."""
        out, lo = [], day_from
        while lo <= day_to:
            hi = min(day_to, lo + timedelta(days=MAX_RANGE_DAYS - 1))
            data = _unwrap(self._call("GET", ENTRIES_PATH, query={RANGE_PARAMS[0]: lo.isoformat(),
                                                                  RANGE_PARAMS[1]: hi.isoformat()})[2])
            out.extend(e for e in data or () if isinstance(e, dict))
            lo = hi + timedelta(days=1)
        return out

    def create(self, payload):
        """(entry, replay): replay is True when FlowApp already had this externalId."""
        status, headers, data = self._call("POST", ENTRIES_PATH, payload)
        replay = (headers.get("Idempotent-Replay") or "").lower() == "true" or status == 200
        return _unwrap(data) or {}, replay

    def patch(self, entry_id, payload):
        return _unwrap(self._call("PATCH", f"{ENTRIES_PATH}/{urllib.parse.quote(str(entry_id), safe='')}",
                                  payload)[2]) or {}


def create_payload(row):
    p = {"startedAt": iso(row["start"]), "stoppedAt": iso(row["stop"]), "comment": row["comment"],
         "externalId": row["external_id"]}
    if row["redmine_id"]:
        p["redmineId"] = row["redmine_id"]
    return p


def patch_payload(row):
    """Only what differs from what FlowApp holds, plus the reason for the change when one is stored."""
    p = {}
    if int(row["start"]) != int(row["sent_start"] or 0):
        p["startedAt"] = iso(row["start"])
    if int(row["stop"]) != int(row["sent_stop"] or 0):
        p["stoppedAt"] = iso(row["stop"])
    if (row["comment"] or "") != (row["sent_comment"] or ""):
        p["comment"] = row["comment"]
    if (row["redmine_id"] or None) != (row["sent_redmine_id"] or None):
        p["redmineId"] = row["redmine_id"]
    if p and row["export_comment"]:
        p["exportComment"] = row["export_comment"]
    return p


FATAL_CODES = ("redmine_key_missing", "unreachable")
MAX_RETRIES = 5


class SendJob:
    """Sends stored blocks one by one in a thread, at most one write per min_interval.

    A block without a FlowApp id is created (its externalId makes a repeat
    harmless), one with an id is patched. A row only ever changes to `sent`
    or `error` after FlowApp answered, so a job cut short leaves nothing
    half-done: sending again replays. 429 waits Retry-After and retries the
    same block; 401, a missing Redmine key or no connection stop the job.
    """

    def __init__(self, conn_factory, client, external_ids, *, min_interval=2.1, sleep=time.sleep,
                 clock=time.monotonic, now=time.time):
        self.id = uuid.uuid4().hex[:12]
        self.conn_factory, self.client = conn_factory, client
        self.external_ids = list(dict.fromkeys(external_ids))
        self.min_interval, self.sleep, self.clock, self.now = min_interval, sleep, clock, now
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self.state = {"id": self.id, "running": False, "total": len(self.external_ids), "done": 0, "failed": 0,
                      "cancelled": False, "errors": [], "fatal": None}

    def start(self):
        with self._lock:
            self.state["running"] = True
        self._thread = threading.Thread(target=self.run, name=f"flowapp-send-{self.id}", daemon=True)
        self._thread.start()
        return self

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def cancel(self):
        self._cancel.set()

    def snapshot(self):
        with self._lock:
            return {**self.state, "errors": list(self.state["errors"]),
                    "fatal": dict(self.state["fatal"]) if self.state["fatal"] else None}

    def _bump(self, failed=False, error=None):
        with self._lock:
            self.state["done"] += 1
            if failed:
                self.state["failed"] += 1
                self.state["errors"].append(error)

    def run(self):
        with self._lock:
            self.state["running"] = True
        conn = None
        try:
            conn = self.conn_factory()
            last = None
            for ext in self.external_ids:
                if self._cancel.is_set():
                    break
                row = _row(conn, ext)
                if row is None or row["status"] in ("sent", "skipped", "merged"):
                    self._bump()
                    continue
                payload = create_payload(row) if row["flowapp_id"] is None else patch_payload(row)
                if not payload:                 # a patch with nothing left to change
                    _update(conn, ext, self.now(), status="sent", error=None)
                    self._bump()
                    continue
                for attempt in range(MAX_RETRIES + 1):
                    if last is not None:
                        wait = self.min_interval - (self.clock() - last)
                        if wait > 0:
                            self.sleep(wait)
                    if self._cancel.is_set():   # cancelled during the pause: this one is not started
                        break
                    last = self.clock()
                    try:
                        if row["flowapp_id"] is None:
                            entry, _ = self.client.create(payload)
                        else:
                            entry = self.client.patch(row["flowapp_id"], payload)
                    except ApiError as e:
                        if e.status == 429 and attempt < MAX_RETRIES:
                            self.sleep(e.retry_after if e.retry_after is not None else 60)
                            last = None         # Retry-After was the pause
                            continue
                        if e.status == 401 or e.code in FATAL_CODES:
                            with self._lock:
                                self.state["fatal"] = {"code": e.code, "message": e.message}
                            return
                        text = e.message + (f" ({e.field})" if e.field else "")
                        mark_error(conn, ext, e.code, text, self.now())
                        self._bump(True, {"external_id": ext, "code": e.code, "message": text})
                        break
                    if not entry.get("id"):
                        if row["flowapp_id"] is None:
                            text = "FlowApp answered without an entry id"
                            mark_error(conn, ext, "bad_answer", text, self.now())
                            self._bump(True, {"external_id": ext, "code": "bad_answer", "message": text})
                            break
                        entry = {**entry, "id": row["flowapp_id"]}
                    mark_sent(conn, ext, entry, self.now())
                    self._bump()
                    break
        except Exception as e:                  # a job must end, never hang as running
            with self._lock:
                self.state["fatal"] = {"code": "internal", "message": str(e)}
        finally:
            if conn is not None:
                conn.close()
            with self._lock:
                self.state["cancelled"] = self._cancel.is_set() and self.state["done"] < self.state["total"]
                self.state["running"] = False
