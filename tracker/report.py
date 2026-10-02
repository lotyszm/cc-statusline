"""Report rows and their table, CSV and Markdown forms."""

import csv
import io
from pathlib import Path

from . import db, ledger

FIELDS = ("day", "client", "project", "branch", "task", "session")


def _field(key, day, name):
    if name == "day":
        return day
    if name == "session":
        return key.session_id
    if name == "project":
        return Path(key.project).name if key.project else None
    return getattr(key, name)


def _sort_key(by):
    def key(row):
        return tuple((row[f] is None, str(row[f] or "").lower()) for f in by)
    return key


def collect(conn, cfg, start, end, by=("client", "task"), client=None, billable_only=False):
    """Rows grouped by the `by` fields, plus totals.

    `seconds` follows the configured overlap mode; `full_seconds` counts every
    session completely, and `clock_seconds` in the totals is real clock time.
    """
    split = ledger.allocate(conn, cfg, start, end, "split")
    full = ledger.allocate(conn, cfg, start, end, "full")
    primary = full if cfg.overlap == "full" else split

    def keep(key):
        if client and (key.client or "").lower() != client.lower():
            return False
        return key.billable or not billable_only

    groups = {}
    for alloc, slot in ((primary, "seconds"), (full, "full_seconds"), (split, "clock_seconds")):
        for (key, day), secs in alloc.items():
            if not keep(key):
                continue
            g = groups.setdefault(tuple(_field(key, day, f) for f in by), {
                "seconds": 0.0, "full_seconds": 0.0, "clock_seconds": 0.0,
                "sessions": set(), "amount": None})
            g[slot] += secs
            g["sessions"].add(key.session_id)
            rate = cfg.rate(key.client) if slot == "seconds" else None
            if rate is not None:
                g["amount"] = (g["amount"] or 0.0) + rate * secs / 3600

    titles = db.task_titles(conn) if "task" in by else {}
    rows = []
    for values, g in groups.items():
        row = dict(zip(by, values))
        if "task" in by:
            row["title"] = titles.get(row["task"]) if row["task"] else None
        row.update(seconds=g["seconds"], full_seconds=g["full_seconds"],
                   clock_seconds=g["clock_seconds"], sessions=len(g["sessions"]), amount=g["amount"])
        rows.append(row)
    rows.sort(key=_sort_key(by))
    totals = {k: sum(r[k] for r in rows) for k in ("seconds", "full_seconds", "clock_seconds")}
    amounts = [r["amount"] for r in rows if r["amount"] is not None]
    totals["amount"] = sum(amounts) if amounts else None
    return rows, totals


def _columns(by, rows):
    cols = list(by)
    if "task" in by:
        cols.append("title")
    return cols


def hours(seconds):
    return f"{seconds / 3600:.2f}"


def hm(seconds):
    h, m = divmod(round(seconds / 60), 60)
    return f"{h}:{m:02d}"


def to_csv(rows, by):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    cols = _columns(by, rows)
    w.writerow(cols + ["minutes", "hours", "full_hours", "sessions", "amount"])
    for r in rows:
        w.writerow([r[c] if r[c] is not None else "" for c in cols] + [
            round(r["seconds"] / 60), hours(r["seconds"]), hours(r["full_seconds"]), r["sessions"],
            f"{r['amount']:.2f}" if r["amount"] is not None else ""])
    return buf.getvalue()


def _cells(rows, totals, by, currency, split_mode):
    cols = _columns(by, rows)
    head = cols + ["hours"] + (["full"] if split_mode else []) + ["sessions"]
    with_amount = totals["amount"] is not None
    if with_amount:
        head.append("amount")
    body = []
    for r in rows:
        line = [("—" if c == "task" else "") if r[c] is None else str(r[c]) for c in cols]
        line.append(hours(r["seconds"]))
        if split_mode:
            line.append(hours(r["full_seconds"]))
        line.append(str(r["sessions"]))
        if with_amount:
            line.append(f"{r['amount']:.2f} {currency}".strip() if r["amount"] is not None else "")
        body.append(line)
    foot = ["total"] + [""] * (len(cols) - 1) + [hours(totals["seconds"])]
    if split_mode:
        foot.append(hours(totals["full_seconds"]))
    foot.append("")
    if with_amount:
        foot.append(f"{totals['amount']:.2f} {currency}".strip())
    return head, body, foot


def to_table(rows, totals, by, title, currency="", split_mode=True):
    head, body, foot = _cells(rows, totals, by, currency, split_mode)
    numeric = {i for i, h in enumerate(head) if h in ("hours", "full", "sessions", "amount")}
    widths = [max(len(line[i]) for line in [head, foot] + body) for i in range(len(head))]

    def fmt(line):
        return "  ".join(c.rjust(w) if i in numeric else c.ljust(w)
                         for i, (c, w) in enumerate(zip(line, widths))).rstrip()

    out = [title, "", fmt(head)]
    out += [fmt(line) for line in body] or ["(no agent time in this range)"]
    out += ["", fmt(foot)]
    if totals["full_seconds"]:
        overlap = 1 - totals["clock_seconds"] / totals["full_seconds"]
        out.append(f"clock time {hm(totals['clock_seconds'])} · all sessions {hm(totals['full_seconds'])}"
                   f" · overlap {overlap:.0%}")
    return "\n".join(out) + "\n"


def to_markdown(rows, totals, by, currency="", split_mode=True):
    head, body, foot = _cells(rows, totals, by, currency, split_mode)
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    lines += ["| " + " | ".join(line) + " |" for line in body + [foot]]
    return "\n".join(lines) + "\n"
