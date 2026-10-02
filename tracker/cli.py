"""Time-tracking commands. statusline.py hands `cc-statusline report`, `task`,
`sessions` and the rest over to main() here."""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, ledger, report, status


# ── dates ────────────────────────────────────────────────────────────────────

def _day(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


def _midnight(d):
    return datetime.combine(d, datetime.min.time()).timestamp()


def resolve_range(args, now):
    """(start, end, label) from the range flags; the current month by default."""
    today = datetime.fromtimestamp(now).date()
    if args.from_ or args.to:
        first = _day(args.from_) if args.from_ else today.replace(day=1)
        last = _day(args.to) if args.to else today
        return _midnight(first), _midnight(last + timedelta(days=1)), f"{first} → {last}"
    if args.today:
        return _midnight(today), now, str(today)
    if args.yesterday:
        y = today - timedelta(days=1)
        return _midnight(y), _midnight(today), str(y)
    if args.week:
        monday = today - timedelta(days=today.weekday())
        return _midnight(monday), now, f"{monday} → {today}"
    if args.last_month:
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        return _midnight(first), _midnight(today.replace(day=1)), first.strftime("%Y-%m")
    first = datetime.strptime(args.month, "%Y-%m").date() if args.month else today.replace(day=1)
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return _midnight(first), min(_midnight(following), now), first.strftime("%Y-%m")


def parse_when(text, now):
    """A moment given as a unix timestamp, 'YYYY-MM-DD HH:MM' or today's 'HH:MM'."""
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            pass
    try:
        t = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        raise ValueError(f"cannot read the time '{text}'") from None
    return datetime.combine(datetime.fromtimestamp(now).date(), t).timestamp()


def _local(ts, fmt="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ts).strftime(fmt) if ts else "-"


def _err(message):
    print(f"cc-statusline: {message}", file=sys.stderr)


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_task(args, conn, cfg, now, out):
    try:
        sid = db.find_session(conn, args.session)
    except LookupError as e:
        _err(e)
        return 2
    sess = db.session(conn, sid)

    if args.action == "show":
        cur = ledger.current(conn, cfg, sid, now)
        print(f"session  {sid}\nproject  {sess['project_dir']}\ncurrent  {(cur.task if cur else None) or '—'}",
              file=out)
        for eff, task, branch in db.assignments(conn, [sid])[sid]:
            print(f"  from {_local(eff)}  {task or 'no task'}" + (f"  (set on {branch})" if branch else ""),
                  file=out)
        return 0

    task = args.task if args.action == "set" else None
    task = cfg.qualify(cfg.classify(sess["project_dir"])[0], task)
    try:
        if getattr(args, "from_start", False):
            effective = sess["first_ts"]
        elif args.since is not None:
            effective = parse_when(args.since, now)
        elif task is not None:
            # The first task a session ever gets covers it from the start: the
            # number usually comes up only after the work has begun.
            cur = ledger.current(conn, cfg, sid, now)
            fresh = not db.assignments(conn, [sid])[sid] and (cur is None or cur.task is None)
            effective = sess["first_ts"] if fresh else now
        else:
            effective = now
    except ValueError as e:
        _err(e)
        return 2

    db.add_assignment(conn, sid, task, effective, "cli", now, branch=db.latest_branch(conn, sid))
    if task and args.title:
        db.set_task_title(conn, task, args.title, now)
    status.write(conn, cfg, sid, now)
    what = f"task {task}" + (f" ({args.title})" if task and args.title else "") if task else "no task"
    print(f"{sid[:8]}: {what} from {_local(effective)}", file=out)
    return 0


def _text(value):
    """A field value as given, '@path' for a file's contents, '-' for stdin."""
    if value is None or value == "":
        return value
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        return Path(value[1:]).expanduser().read_text(encoding="utf-8")
    return value


def _task_fields(args):
    return {f: _text(getattr(args, f)) for f in ("title", *db.TASK_DETAILS)}


def cmd_tasks(args, conn, cfg, now, out):
    if args.action == "list":
        rows = [r for r in db.task_details(conn).values()
                if (not args.client or r["task"].lower().startswith(args.client.lower() + ":"))
                and (not args.status or (r["status"] or "").lower() == args.status.lower())]
        if not rows:
            print("no tasks", file=out)
            return 0
        width = max(len(r["task"]) for r in rows)
        swidth = max(len(r["status"] or "—") for r in rows)
        for r in sorted(rows, key=lambda r: r["task"]):
            print(f"{r['task'].ljust(width)}  {(r['status'] or '—').ljust(swidth)}  {r['title'] or ''}".rstrip(),
                  file=out)
        return 0

    if args.action == "show":
        r = db.task_details(conn, args.task)
        if r is None:
            _err(f"no task '{args.task}'")
            return 2
        print(f"task     {r['task']}\ntitle    {r['title'] or '—'}\nstatus   {r['status'] or '—'}\n"
              f"url      {r['url'] or '—'}\nupdated  {_local(r['updated_at'])}", file=out)
        for name in ("description", "plan"):
            if r[name]:
                print(f"\n{name}\n{r[name].rstrip()}", file=out)
        return 0

    if args.action == "set":
        try:
            fields = _task_fields(args)
        except OSError as e:
            _err(e)
            return 2
        if all(v is None for v in fields.values()):
            _err("nothing to set: give --title, --description, --plan, --status or --url")
            return 2
        db.set_task(conn, args.task, now, **fields)
        print(f"task {args.task} updated", file=out)
        return 0

    # import: one JSON object per line, {"task": ..., "title": ..., "plan": ...}
    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).expanduser().read_text(encoding="utf-8")
    except OSError as e:
        _err(e)
        return 2
    allowed = ("title", *db.TASK_DETAILS)
    count = 0
    conn.execute("BEGIN")
    try:
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                task = str(item["task"]).strip()
                if not task or not isinstance(item, dict):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise ValueError(f"line {n}: expected a JSON object with a \"task\"") from None
            db.set_task(conn, task, now, **{k: item[k] for k in allowed if item.get(k) is not None})
            count += 1
    except ValueError as e:
        conn.execute("ROLLBACK")
        _err(f"{args.file}: {e}; nothing imported")
        return 2
    conn.execute("COMMIT")
    print(f"imported {count} tasks", file=out)
    return 0


def cmd_report(args, conn, cfg, now, out):
    by = tuple(f.strip() for f in args.by.split(",") if f.strip())
    unknown = [f for f in by if f not in report.FIELDS]
    if unknown or not by:
        _err(f"--by takes {', '.join(report.FIELDS)}")
        return 2
    start, end, label = resolve_range(args, now)
    rows, totals = report.collect(conn, cfg, start, end, by, args.client, args.billable)
    split_mode = cfg.overlap == "split"
    if args.format == "csv":
        out.write(report.to_csv(rows, by))
    elif args.format == "md":
        out.write(report.to_markdown(rows, totals, by, cfg.currency, split_mode))
    else:
        title = (f"cc-statusline · {label} · overlap {cfg.overlap} · break after "
                 f"{cfg.idle / 60:g} min" + (f" · client {args.client}" if args.client else ""))
        out.write(report.to_table(rows, totals, by, title, cfg.currency, split_mode))
    return 0


def cmd_sessions(args, conn, cfg, now, out):
    start, end, _ = resolve_range(args, now)
    per = {}
    for (key, _day), secs in ledger.allocate(conn, cfg, start, end).items():
        if args.client and (key.client or "").lower() != args.client.lower():
            continue
        p = per.setdefault(key.session_id, {"seconds": 0.0, "unassigned": 0.0, "tasks": set(), "key": key})
        p["seconds"] += secs
        if key.task:
            p["tasks"].add(key.task)
        elif key.billable:
            p["unassigned"] += secs
    if args.unassigned:
        per = {sid: p for sid, p in per.items() if p["unassigned"] >= 60}
    info = {r["session_id"]: r for r in conn.execute("SELECT * FROM sessions")} if per else {}

    head = ["session", "started", "client", "project", "branch", "task", "time", "no task", "title"]
    lines = []
    for sid, p in sorted(per.items(), key=lambda kv: (info.get(kv[0]) or {"first_ts": 0})["first_ts"] or 0):
        k, s = p["key"], info.get(sid)
        lines.append([sid[:8], _local(s["first_ts"] if s else None), k.client or "—",
                      Path(k.project).name if k.project else "—", k.branch or "—",
                      ",".join(sorted(p["tasks"])) or "—", report.hm(p["seconds"]),
                      report.hm(p["unassigned"]) if p["unassigned"] else "",
                      ((s["title"] if s else None) or "")[:48]])
    if not lines:
        print("no sessions in this range" + (" with time missing a task" if args.unassigned else ""), file=out)
        return 0
    widths = [max(len(r[i]) for r in [head] + lines) for i in range(len(head))]
    for r in [head] + lines:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip(), file=out)
    if args.unassigned:
        print("\nassign with: cc-statusline task set <TASK> --session <session> --from-start", file=out)
    return 0


def cmd_explain(args, conn, cfg, now, out):
    try:
        sid = db.find_session(conn, args.session)
    except LookupError as e:
        _err(e)
        return 2
    start = end = None
    if args.day:
        first = _day(args.day)
        start, end = _midnight(first), _midnight(first + timedelta(days=1))
    b = ledger.breakdown(conn, cfg, sid, start, end)
    s = b["session"]
    project = Path(s["project_dir"]).name if s["project_dir"] else "—"
    print(f"session {s['short']} · {s['client'] or 'no client'}{'' if s['billable'] else ' (not billable)'}"
          f" · {project} · {_local(s['first'])} → {_local(s['last'])}", file=out)
    if s["title"]:
        print(f"“{s['title']}”", file=out)
    print("", file=out)
    day = None
    for blk in b["blocks"]:
        this_day = _local(blk["start"], "%Y-%m-%d")
        if this_day != day:
            day = this_day
            print(f"{day}", file=out)
        span = f"{_local(blk['start'], '%H:%M')}–{_local(blk['end'], '%H:%M')}"
        if blk["kind"] == "break":
            print(f"  {span}  {report.hm(blk['seconds']):>5}  break  {blk['note']}", file=out)
            continue
        line = f"  {span}  {report.hm(blk['seconds']):>5}  work   {blk['task'] or 'no task'}"
        if abs(blk["counted"] - blk["seconds"]) >= 30:
            line += f"   (counted {report.hm(blk['counted'])}: parallel sessions share the clock)"
        print(line, file=out)
    t = b["totals"]
    print(f"\ncredited {report.hm(t['credited'])} · counted {report.hm(t['counted'])} "
          f"({'split with parallel sessions' if b['overlap'] == 'split' else 'counted fully'}) · "
          f"breaks {report.hm(t['breaks'])} · a break starts after {cfg.idle / 60:g} min without activity",
          file=out)
    return 0


def cmd_status(args, conn, cfg, now, out):
    try:
        sid = db.find_session(conn, args.session)
    except LookupError as e:
        _err(e)
        return 2
    out.write(json.dumps(status.write(conn, cfg, sid, now), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_import(args, conn, cfg, now, out):
    from . import importer
    roots = [Path(p).expanduser() for p in args.paths] if args.paths else importer.default_roots()
    stats = importer.run(conn, roots, progress=lambda m: print(m, file=out), force=args.force)
    print(f"imported {stats['events']} events from {stats['files']} files "
          f"({stats['skipped_files']} unchanged, {stats['skipped_sessions']} sessions already tracked by hooks)",
          file=out)
    return 0


def cmd_dashboard(args, conn, cfg, now, out):
    from . import dashboard
    conn.close()
    return dashboard.serve(args.port, open_browser=not (args.no_open or args.service), out=out,
                           service=args.service)


# ── parser ───────────────────────────────────────────────────────────────────

def _add_range(p):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--today", action="store_true")
    g.add_argument("--yesterday", action="store_true")
    g.add_argument("--week", action="store_true", help="since Monday")
    g.add_argument("--month", nargs="?", const="", metavar="YYYY-MM", help="a calendar month (default: this one)")
    g.add_argument("--last-month", action="store_true")
    p.add_argument("--from", dest="from_", metavar="YYYY-MM-DD")
    p.add_argument("--to", metavar="YYYY-MM-DD", help="inclusive")


def build_parser():
    p = argparse.ArgumentParser(prog="cc-statusline", description="Time spent working with Claude Code, "
                                "per client, project and task.")
    sub = p.add_subparsers(dest="command", metavar="command")

    sub.add_parser("hook", help="handle a Claude Code hook event (JSON on stdin)")

    r = sub.add_parser("report", help="hours per client and task")
    _add_range(r)
    r.add_argument("--by", default="client,task", help=f"grouping, any of: {', '.join(report.FIELDS)}")
    r.add_argument("--client")
    r.add_argument("--billable", action="store_true", help="only billable projects")
    r.add_argument("--format", choices=("table", "csv", "md"), default="table")
    r.set_defaults(func=cmd_report)

    s = sub.add_parser("sessions", help="sessions and their time")
    _add_range(s)
    s.add_argument("--client")
    s.add_argument("--unassigned", action="store_true", help="only billable time without a task")
    s.set_defaults(func=cmd_sessions)

    t = sub.add_parser("task", help="set, clear or show the task of a session")
    tsub = t.add_subparsers(dest="action", metavar="action", required=True)
    ts = tsub.add_parser("set", help="log a session's time to a task")
    ts.add_argument("task")
    ts.add_argument("--session", required=True, help="id or unique prefix")
    ts.add_argument("--title")
    when = ts.add_mutually_exclusive_group()
    when.add_argument("--from-start", action="store_true", help="from the start of the session")
    when.add_argument("--since", help="unix time, 'YYYY-MM-DD HH:MM' or 'HH:MM'")
    tc = tsub.add_parser("clear", help="this session's time is not for any task")
    tc.add_argument("--session", required=True)
    tc.add_argument("--since")
    tw = tsub.add_parser("show", help="current task and history")
    tw.add_argument("--session", required=True)
    t.set_defaults(func=cmd_task)

    k = sub.add_parser("tasks", help="the tasks themselves: titles, descriptions, plans")
    ksub = k.add_subparsers(dest="action", metavar="action", required=True)
    kl = ksub.add_parser("list", help="every known task")
    kl.add_argument("--client", help="only keys starting with 'client:'")
    kl.add_argument("--status")
    kw = ksub.add_parser("show", help="one task in full")
    kw.add_argument("task")
    kset = ksub.add_parser("set", help="describe a task; fields not given are kept")
    kset.add_argument("task")
    for name in ("title", *db.TASK_DETAILS):
        kset.add_argument(f"--{name}", help="text, @file or - for stdin" if name in ("description", "plan") else None)
    ki = ksub.add_parser("import", help="add or update tasks from JSON Lines")
    ki.add_argument("file", help=f"one object per line with \"task\" and any of: title, {', '.join(db.TASK_DETAILS)}"
                                 "; - for stdin")
    k.set_defaults(func=cmd_tasks)

    ex = sub.add_parser("explain", help="how a session's time was counted, block by block")
    ex.add_argument("--session", required=True, help="id or unique prefix")
    ex.add_argument("--day", metavar="YYYY-MM-DD", help="only this day")
    ex.set_defaults(func=cmd_explain)

    st = sub.add_parser("status", help="recompute and print a session's status-line figures")
    st.add_argument("--session", required=True)
    st.set_defaults(func=cmd_status)

    i = sub.add_parser("import", help="backfill from Claude Code transcripts")
    i.add_argument("paths", nargs="*", help="account dirs or projects/ dirs (default: every ~/.claude*)")
    i.add_argument("--force", action="store_true", help="read every file again, even unchanged ones")
    i.set_defaults(func=cmd_import)

    d = sub.add_parser("dashboard", help="local web dashboard")
    d.add_argument("--port", type=int, default=8765)
    d.add_argument("--no-open", action="store_true", help="do not open a browser")
    d.add_argument("--service", action="store_true",
                   help="run as the login service: no browser, exit quietly when the port is taken")
    d.set_defaults(func=cmd_dashboard)

    return p


def main(argv=None, now=None, out=None):
    out = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(out)
        return 0
    if args.command == "hook":
        from . import hook
        return hook.main()
    cfg = config.load()
    if cfg.error:
        _err(f"config ignored, using defaults: {cfg.error}")
    conn = db.connect()
    try:
        return args.func(args, conn, cfg, now or time.time(), out)
    finally:
        conn.close()
