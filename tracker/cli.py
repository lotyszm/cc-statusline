"""Time-tracking commands. statusline.py hands `cc-statusline report`, `task`,
`sessions` and the rest over to main() here."""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, ledger, report, status, worklist


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


LIST_FLAGS = {"pitfalls": "pitfall", "files": "file", "commits": "commit", "depends_on": "depends_on"}
LONG_FIELDS = ("description", "plan", "evidence", "next_step", "outcome", "tests", "criteria")


def _task_fields(args):
    fields = {f: _text(getattr(args, f, None)) for f in ("title", *db.TASK_DETAILS) if f != "project"}
    for field, flag in LIST_FLAGS.items():
        fields[field] = getattr(args, flag, None) or None
    return fields


def _author(args):
    return getattr(args, "author", None) or os.environ.get("CC_STATUSLINE_AUTHOR") or "cli"


def _here(conn):
    return worklist.find_project(conn, os.getcwd())


def _resolve(conn, args, ref):
    proj = worklist.project(conn, args.project) if getattr(args, "project", None) else _here(conn)
    return worklist.resolve(conn, ref, proj)


def _session_arg(conn, args):
    """The session to log time to: --session, else this Claude Code session."""
    ref = getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not ref:
        return None
    try:
        return db.find_session(conn, ref)
    except LookupError:
        return None


def _hm(seconds):
    return report.hm(seconds) if seconds else "—"


def _items(value):
    try:
        return json.loads(value) if value else []
    except ValueError:
        return [value]


def _ticket_url(conn, r):
    if r["url"]:
        return r["url"]
    proj = worklist.project(conn, r["project"]) if r["project"] else None
    if r["ticket"] and proj is not None and proj["ticket_url"]:
        return proj["ticket_url"].replace("{ticket}", r["ticket"])
    return None


def _show(conn, cfg, r, out, full=False):
    """Everything needed to pick a task up again, most useful first."""
    key = r["task"]
    secs = worklist.seconds_per_task(conn, cfg).get(key, 0.0)
    sess = worklist.sessions_of(conn, key)
    head = [("task", key), ("title", r["title"] or "—"), ("status", r["status"] or "—")]
    if r["number"] is not None:
        head += [("kind", r["kind"] or "task"), ("priority", r["priority"] or "—"), ("area", r["area"] or "—"),
                 ("ticket", r["ticket"] or "—")]
    head += [("url", _ticket_url(conn, r) or "—"), ("time", f"{_hm(secs)} in {len(sess)} sessions"),
             ("created", _local(r["created_at"])), ("closed", _local(r["closed_at"])),
             ("updated", _local(r["updated_at"]))]
    for label, value in head:
        print(f"{label.ljust(8)} {value}", file=out)
    for name in ("next_step", "criteria", "description", "evidence", "plan", "tests", "outcome"):
        if r[name]:
            print(f"\n{name.replace('_', ' ')}\n{r[name].rstrip()}", file=out)
    for name in db.TASK_LISTS:
        items = _items(r[name])
        if items:
            print(f"\n{name.replace('_', ' ')}", file=out)
            for item in items:
                print(f"  - {item}", file=out)
    ms = worklist.metrics(conn, key)
    if ms:
        print("\nmetrics", file=out)
        for m in ms:
            print(f"  {m['name']}: {m['before'] or '—'} → {m['after'] or '—'}  ({m['method']})", file=out)
    ns = worklist.notes(conn, key)
    if ns:
        shown = ns if full else ns[-5:]
        print(f"\nnotes" + ("" if full or len(ns) <= 5 else f" (last 5 of {len(ns)}, --all for every one)"),
              file=out)
        for n in shown:
            print(f"  {_local(n['ts'])}  {n['text']}", file=out)
    if sess:
        print("\nsessions", file=out)
        for s in (sess if full else sess[-5:]):
            print(f"  {s['session_id'][:8]}  {_local(s['first_ts'])}  {(s['title'] or '')[:60]}", file=out)


def cmd_tasks(args, conn, cfg, now, out):
    if args.action == "list":
        statuses = [x.strip() for x in args.status.split(",")] if args.status else None
        if args.open:
            statuses = list(db.OPEN_STATUSES)
        if args.project or args.kind or args.area or args.priority or args.open:
            proj = args.project
            if proj == ".":
                here = _here(conn)
                proj = here["slug"] if here else "\0"
            rows = worklist.tasks(conn, proj, statuses, args.kind, args.area, args.priority)
        else:
            rows = [r for r in db.task_details(conn).values()
                    if not statuses or (r["status"] or "").lower() in [x.lower() for x in statuses]]
        rows = [r for r in rows if not args.client or r["task"].lower().startswith(args.client.lower() + ":")]
        if not rows:
            print("no tasks", file=out)
            return 0
        secs = worklist.seconds_per_task(conn, cfg) if args.time else {}
        width = max(len(r["task"]) for r in rows)
        swidth = max(len(r["status"] or "—") for r in rows)
        for r in sorted(rows, key=lambda r: (r["project"] or "", r["number"] or 0, r["task"])):
            line = f"{r['task'].ljust(width)}  {(r['status'] or '—').ljust(swidth)}"
            if args.time:
                line += f"  {_hm(secs.get(r['task'], 0)):>6}"
            line += f"  {r['title'] or ''}"
            if args.next and r["next_step"]:
                line += f"\n{' ' * (width + 2)}→ {r['next_step'].splitlines()[0][:120]}"
            print(line.rstrip(), file=out)
        return 0

    if args.action == "projects":
        rows = conn.execute("SELECT p.*, (SELECT count(*) FROM tasks t WHERE t.project = p.slug) AS n, "
                            "(SELECT count(*) FROM tasks t WHERE t.project = p.slug AND t.status IN "
                            f"({', '.join('?' * len(db.OPEN_STATUSES))})) AS open FROM projects p ORDER BY client, slug",
                            db.OPEN_STATUSES).fetchall()
        for r in rows:
            print(f"{r['slug']:<28} {r['client'] or '—':<12} {r['open']:>4} open / {r['n']:<5} "
                  f"{r['remote'] or r['path'] or ''}", file=out)
        return 0 if rows else (print("no projects", file=out) or 0)

    if args.action == "project":
        if worklist.project(conn, args.slug) is None:
            if not args.path:
                _err(f"no project '{args.slug}'; give --path to create it")
                return 2
            conn.execute("INSERT INTO projects (slug, path, created_at) VALUES (?, ?, ?)",
                         (args.slug, str(Path(args.path).expanduser().resolve()), now))
            if not args.client:
                args.client = cfg.classify(args.path)[0]
        worklist.set_project(conn, args.slug, client=args.client, remote=args.remote,
                             path=str(Path(args.path).expanduser().resolve()) if args.path else None,
                             ticket_url=args.ticket_url, repo_url=args.repo_url)
        print(f"project {args.slug} saved", file=out)
        return 0

    if args.action == "hist":
        key = _resolve(conn, args, args.task) if args.task else None
        if args.task and key is None:
            _err(f"no task '{args.task}'")
            return 2
        since = now - args.days * 86400 if args.days else None
        rows = worklist.history(conn, key, since)
        for h in rows:
            change = h["after"] if h["before"] is None else f"{h['before']} → {h['after']}"
            print(f"{_local(h['ts'])}  {h['task']}  {h['field']}: {(change or '')[:100]}"
                  + (f"  ({h['author']})" if h["author"] else ""), file=out)
        return 0 if rows else (print("no changes", file=out) or 0)

    if args.action == "add":
        proj = worklist.project(conn, args.project) if args.project else worklist.ensure_project(conn, cfg, os.getcwd(), now)
        if proj is None:
            _err(f"no project '{args.project}'")
            return 2
        try:
            fields = _task_fields(args)
        except OSError as e:
            _err(e)
            return 2
        fields = {k: (json.dumps(v, ensure_ascii=False) if k in db.TASK_LISTS else v)
                  for k, v in fields.items() if v is not None and k not in ("title", "project")}
        fields.setdefault("status", "open")
        key = worklist.add(conn, proj, args.title, now, _author(args), kind=fields.pop("kind", None) or "task",
                           status=fields.pop("status"), **fields)
        print(key, file=out)
        return 0

    if args.action == "set" and _resolve(conn, args, args.task) is None:
        # Not a work-list task: a plain key, as `task set` uses them.
        try:
            fields = _task_fields(args)
        except OSError as e:
            _err(e)
            return 2
        if all(v is None for v in fields.values()):
            _err("nothing to set: give --title, --description, --plan, --status or --url")
            return 2
        fields = {k: (json.dumps(v, ensure_ascii=False) if k in db.TASK_LISTS and v else v) for k, v in fields.items()}
        db.set_task(conn, args.task, now)
        worklist.update(conn, args.task, now, _author(args), **fields)
        print(f"task {args.task} updated", file=out)
        return 0

    if args.action in ("show", "set", "start", "done", "note", "metric"):
        key = _resolve(conn, args, args.task)
        if key is None:
            _err(f"no task '{args.task}'")
            return 2
        r = db.task_details(conn, key)

        if args.action == "show":
            _show(conn, cfg, r, out, full=args.all)
            return 0

        if args.action == "note":
            worklist.note(conn, key, _text(args.text), now, _author(args))
            print(f"{key}: note added", file=out)
            return 0

        if args.action == "metric":
            try:
                worklist.metric(conn, key, args.name, args.before, args.after, args.method, now)
            except ValueError as e:
                _err(e)
                return 2
            print(f"{key}: {args.name} {args.before} → {args.after}", file=out)
            return 0

        try:
            fields = _task_fields(args)
        except OSError as e:
            _err(e)
            return 2

        if args.action == "set":
            if all(v is None for v in fields.values()):
                _err("nothing to set: give --title, --description, --plan, --status or --url")
                return 2
            changed = worklist.update(conn, key, now, _author(args), **fields)
            print(f"task {key} updated" + ("" if changed else " (no change)"), file=out)
            return 0

        if args.action == "start":
            if r["status"] not in db.OPEN_STATUSES and r["status"] is not None and not args.reopen:
                _err(f"{key} is {r['status']}; --reopen to work on it again")
                return 2
            fields["status"] = "in-progress"
            worklist.update(conn, key, now, _author(args), **fields)
            sid = _session_arg(conn, args)
            if sid is None:
                print(f"{key}: in progress (no session to log time to; give --session)", file=out)
                return 0
            since = parse_when(args.since, now) if args.since else (sess_first(conn, sid) if args.from_start else None)
            since = worklist.assign_session(conn, cfg, sid, key, now, since)
            status.write(conn, cfg, sid, now)
            print(f"{key}: in progress, time of {sid[:8]} from {_local(since)}", file=out)
            return 0

        # done
        for m in args.metric or []:
            try:
                worklist.metric(conn, key, *m, now=now)
            except ValueError as e:
                _err(e)
                return 2
        try:
            worklist.close(conn, key, "parked" if args.park else "done", now=now, author=_author(args),
                           **{k: v for k, v in fields.items() if k != "status"})
        except ValueError as e:
            _err(f"{key}: {e} (--outcome)")
            return 2
        if not (args.metric or worklist.metrics(conn, key)):
            print(f"{key}: closed without a metric; if anything was measured, add it with "
                  f"'tasks metric {key} NAME BEFORE AFTER --method HOW'", file=sys.stderr)
        print(f"{key}: {'parked' if args.park else 'done'}", file=out)
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


def sess_first(conn, sid):
    return db.session(conn, sid)["first_ts"]


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

    k = sub.add_parser("tasks", help="the work list: tasks, their descriptions, plans, notes and history")
    ksub = k.add_subparsers(dest="action", metavar="action", required=True)

    def fields(q, title=True):
        if title:
            q.add_argument("--title")
        for name in db.TASK_DETAILS:
            if name == "project":       # fixed by the key; --project picks where #N is looked up
                continue
            q.add_argument(f"--{name.replace('_', '-')}", dest=name,
                           help="text, @file or - for stdin" if name in LONG_FIELDS else None)
        for field, flag in LIST_FLAGS.items():
            q.add_argument(f"--{flag.replace('_', '-')}", dest=flag, action="append",
                           help=f"add to {field.replace('_', ' ')} (repeatable)")
        q.add_argument("--author", help="who made the change (default $CC_STATUSLINE_AUTHOR or 'cli')")

    def ref(q):
        q.add_argument("task", help="key, ticket, #N in this repo's project or project#N")
        q.add_argument("--project", help="project slug for #N (default: the one of the current directory)")

    kl = ksub.add_parser("list", help="tasks, filtered")
    kl.add_argument("--client", help="only keys starting with 'client:'")
    kl.add_argument("--status", help="comma-separated")
    kl.add_argument("--open", action="store_true", help=f"only {', '.join(db.OPEN_STATUSES)}")
    kl.add_argument("--project", help="a project slug, or . for the current directory's")
    kl.add_argument("--kind")
    kl.add_argument("--area")
    kl.add_argument("--priority")
    kl.add_argument("--time", action="store_true", help="counted time per task (reads the whole history)")
    kl.add_argument("--next", action="store_true", help="the next step under each task")
    kw = ksub.add_parser("show", help="one task in full: what to know to pick it up again")
    ref(kw)
    kw.add_argument("--all", action="store_true", help="every note and session, not the last five")
    kset = ksub.add_parser("set", help="change fields; others are kept, lists are appended to")
    ref(kset)
    fields(kset)
    ka = ksub.add_parser("add", help="a new task in the current directory's project")
    ka.add_argument("title")
    ka.add_argument("--project", help="project slug (default: the current directory's, created on first use)")
    fields(ka, title=False)
    kst = ksub.add_parser("start", help="mark in progress and log this session's time to it")
    ref(kst)
    fields(kst)
    kst.add_argument("--session", help="id or prefix (default $CLAUDE_CODE_SESSION_ID)")
    kst.add_argument("--reopen", action="store_true", help="start a task that is done or parked")
    when = kst.add_mutually_exclusive_group()
    when.add_argument("--from-start", action="store_true")
    when.add_argument("--since")
    kd = ksub.add_parser("done", help="close a task; needs --outcome")
    ref(kd)
    fields(kd)
    kd.add_argument("--park", action="store_true", help="set aside instead of done")
    kd.add_argument("--metric", nargs=4, action="append", metavar=("NAME", "BEFORE", "AFTER", "METHOD"))
    kn = ksub.add_parser("note", help="add a dated note")
    ref(kn)
    kn.add_argument("text", help="text, @file or - for stdin")
    kn.add_argument("--author")
    km = ksub.add_parser("metric", help="record a measurement: before → after, and how it was measured")
    ref(km)
    km.add_argument("name")
    km.add_argument("before")
    km.add_argument("after")
    km.add_argument("--method", help="how it was measured (required)")
    kh = ksub.add_parser("hist", help="what changed, newest last")
    kh.add_argument("task", nargs="?")
    kh.add_argument("--project")
    kh.add_argument("--days", type=float, help="only the last N days")
    ksub.add_parser("projects", help="projects and their open tasks")
    kp = ksub.add_parser("project", help="create or change a project")
    kp.add_argument("slug")
    for name in ("client", "remote", "path", "ticket-url", "repo-url"):
        kp.add_argument(f"--{name}", help="URL with {ticket}" if name == "ticket-url" else None)
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
