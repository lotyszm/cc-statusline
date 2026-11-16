"""Local web dashboard: one page and a small JSON API, on 127.0.0.1 only.

Each request reads the config and the database afresh, so the page always
agrees with the CLI. The writes - assigning a session, changing a task's
status, moving a task to another project - need a custom header, which a page
from another origin cannot send without a CORS preflight this server never
approves, and the Host check stops DNS rebinding.
"""

import json
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db, flowapp, ledger, paths, status, worklist

PAGE = Path(__file__).resolve().parent / "static" / "dashboard.html"
TASKS_PAGE = Path(__file__).resolve().parent / "static" / "tasks.html"
FLOWAPP_PAGE = Path(__file__).resolve().parent / "static" / "flowapp.html"
MAX_BODY = 64 * 1024
FLOWAPP_POSTS = ("/api/flowapp/draft", "/api/flowapp/send", "/api/flowapp/job/cancel")


def _range(query, now):
    today = datetime.fromtimestamp(now).date()

    def day(name, default):
        try:
            return datetime.strptime(query.get(name, [""])[0], "%Y-%m-%d").date()
        except ValueError:
            return default

    first, last = day("from", today.replace(day=1)), day("to", today)
    if last < first:
        first, last = last, first
    start = datetime.combine(first, datetime.min.time()).timestamp()
    end = min(datetime.combine(last + timedelta(days=1), datetime.min.time()).timestamp(), now)
    return first, last, start, end


def _add(d, key, value):
    d[key] = (d.get(key) or 0.0) + value


def build_report(conn, cfg, start, end):
    """Everything the page shows for one date range, as plain JSON data."""
    split = ledger.allocate(conn, cfg, start, end, "split")
    full = ledger.allocate(conn, cfg, start, end, "full")
    primary = full if cfg.overlap == "full" else split
    details = db.task_details(conn)

    clients, sessions = {}, {}
    days = defaultdict(lambda: defaultdict(float))      # day -> (client, billable, assigned) -> s
    totals = {"seconds": 0.0, "full_seconds": sum(full.values()), "clock_seconds": sum(split.values()),
              "billable_seconds": 0.0, "unassigned_seconds": 0.0, "amount": None}

    for (key, day), secs in primary.items():
        c = clients.setdefault(key.client, {"client": key.client, "billable": False, "seconds": 0.0,
                                            "full_seconds": 0.0, "amount": None, "tasks": {}})
        c["billable"] = c["billable"] or key.billable
        d = details.get(key.task)
        t = c["tasks"].setdefault(key.task, {"task": key.task, "title": d["title"] if d else None,
                                             "number": d["number"] if d else None,
                                             **{f: d[f] if d else None for f in db.TASK_DETAILS},
                                             "seconds": 0.0, "full_seconds": 0.0, "amount": None,
                                             "sessions": set(), "days": defaultdict(float)})
        c["seconds"] += secs
        t["seconds"] += secs
        t["sessions"].add(key.session_id)
        t["days"][day] += secs
        rate = cfg.rate(key.client)
        if rate is not None:
            for bucket in (t, c, totals):
                _add(bucket, "amount", rate * secs / 3600)
        days[day][(key.client, key.billable, key.task is not None)] += secs
        s = sessions.setdefault(key.session_id, {"seconds": 0.0, "unassigned_seconds": 0.0,
                                                 "tasks": set(), "key": key})
        s["seconds"] += secs
        if key.task:
            s["tasks"].add(key.task)
        elif key.billable:
            s["unassigned_seconds"] += secs
            totals["unassigned_seconds"] += secs
        if key.billable:
            totals["billable_seconds"] += secs
        totals["seconds"] += secs

    for (key, _), secs in full.items():
        c = clients.get(key.client)
        if c:
            c["full_seconds"] += secs
            if key.task in c["tasks"]:
                c["tasks"][key.task]["full_seconds"] += secs

    info = {}
    if sessions:
        ids = list(sessions)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(f"SELECT * FROM sessions WHERE session_id IN ({', '.join('?' * len(chunk))})",
                                  chunk):
                info[r["session_id"]] = r

    def started(sid):
        row = info.get(sid)
        return (row["first_ts"] if row else None) or 0

    def task_rows(tasks):
        rows = []
        for t in tasks.values():
            days = {d: s for d, s in sorted(t["days"].items()) if s >= 1}
            sids = sorted(t["sessions"], key=started)
            # Session titles say what was done: the raw material for an invoice line.
            names = []
            for sid in sids:
                title = info[sid]["title"] if sid in info else None
                if title and title not in names:
                    names.append(title)
            rows.append(dict(t, sessions=len(sids), days=days, first_day=min(days) if days else None,
                             last_day=max(days) if days else None, titles=names[:12]))
        return sorted(rows, key=lambda t: (t["task"] is None, -t["seconds"]))

    out_sessions = []
    for sid, s in sessions.items():
        k, row = s["key"], info.get(sid)
        out_sessions.append({
            "session_id": sid, "short": sid[:8], "started": row["first_ts"] if row else None,
            "last": row["last_ts"] if row else None, "title": row["title"] if row else None,
            "account": row["account"] if row else None, "client": k.client, "billable": k.billable,
            "project": Path(k.project).name if k.project else None, "project_path": k.project,
            "branch": k.branch, "tasks": sorted(s["tasks"]), "seconds": s["seconds"],
            "unassigned_seconds": s["unassigned_seconds"]})
    out_sessions.sort(key=lambda s: s["started"] or 0, reverse=True)

    def client_row(c):
        tasks = task_rows(c["tasks"])
        days = {d for t in tasks for d in t["days"]}
        sessions = {sid for t in c["tasks"].values() for sid in t["sessions"]}
        return dict(c, tasks=tasks, sessions=len(sessions), days_count=len(days),
                    last_day=max(days) if days else None)

    return {
        "totals": totals,
        "clients": sorted((client_row(c) for c in clients.values()), key=lambda c: -c["seconds"]),
        "days": [{"day": d, "rows": [{"client": c, "billable": b, "assigned": a, "seconds": s}
                                     for (c, b, a), s in v.items()]}
                 for d, v in sorted(days.items())],
        "sessions": out_sessions,
    }


# Status groups the work-list view filters by; a single status works too.
TASK_VIEWS = {
    "open": ("task", worklist.OPEN_STATUSES),
    "waiting": ("task", ("waiting",)),
    "done": ("task", ("done",)),
    "parked": ("task", ("parked",)),
    "decisions": ("decision", None),
    "all": (None, None),
}
TASK_FIELDS = ("task", "number", "project", "title", "kind", "status", "priority", "area", "ticket",
               "next_step", "created_at", "updated_at", "closed_at")


def _one(query, name):
    return (query.get(name, [""])[0] or "").strip()


def build_tasks(conn, cfg, query, now=None):
    """The work list for the page: tasks filtered by project, view and text, with their time."""
    view = _one(query, "view") or "open"
    kind, statuses = TASK_VIEWS.get(view, (None, (view,)))
    proj = _one(query, "project") or None
    text = _one(query, "q").lower()
    projects = {p["slug"]: p for p in conn.execute("SELECT * FROM projects")}
    rows = worklist.tasks(conn, proj, statuses, kind)
    if text:
        rows = [r for r in rows if any(text in (r[f] or "").lower()
                                       for f in ("task", "title", "ticket", "next_step", "description"))]
    seconds = worklist.seconds_per_task(conn, cfg, end=now)
    open_counts = defaultdict(int)
    for r in conn.execute("SELECT project, count(*) n FROM tasks WHERE number IS NOT NULL "
                          "AND coalesce(kind, 'task') = 'task' "
                          f"AND status IN ({', '.join('?' * len(worklist.OPEN_STATUSES))}) GROUP BY project",
                          worklist.OPEN_STATUSES):
        open_counts[r["project"]] = r["n"]
    tasks = []
    for r in rows:
        p = projects.get(r["project"])
        tasks.append({**{f: r[f] for f in TASK_FIELDS}, "kind": r["kind"] or "task",
                      "client": p["client"] if p else None, "ticket_url": worklist.ticket_url(conn, r),
                      "seconds": seconds.get(r["task"], 0.0)})
    return {
        "view": view, "project": proj, "q": text,
        "projects": sorted(({"slug": slug, "client": p["client"], "open": open_counts.get(slug, 0)}
                            for slug, p in projects.items()), key=lambda p: (-p["open"], p["slug"])),
        "tasks": tasks,
    }


def build_task(conn, cfg, key, now=None):
    """One task in full, as `tasks show` prints it: fields, lists, metrics, notes, history, sessions."""
    row = db.task_details(conn, key)
    if row is None:
        raise LookupError(f"no task {key}")
    secs, sessions = worklist.time_of(conn, cfg, key, end=now)
    p = worklist.project(conn, row["project"]) if row["project"] else None
    out = {k: row[k] for k in row.keys() if k not in db.TASK_LISTS}
    out.update({f: worklist.items(row[f]) for f in db.TASK_LISTS})
    out.update({
        "kind": row["kind"] or "task", "client": p["client"] if p else None,
        "ticket_url": worklist.ticket_url(conn, row), "seconds": secs,
        "statuses": list(worklist.STATUSES.get(row["kind"] or "task", ())),
        "moved_from": [r["old_task"] for r in conn.execute(
            "SELECT old_task FROM task_moves WHERE new_task = ? ORDER BY ts", (key,))],
        "metrics": [dict(m) for m in worklist.metrics(conn, key)],
        "notes": [dict(n) for n in worklist.notes(conn, key)],
        "history": [dict(h) for h in worklist.history(conn, key)][-100:],
        "sessions": [{"session_id": s["session_id"], "short": s["session_id"][:8], "started": s["first_ts"],
                      "title": s["title"]} for s in sessions],
    })
    return out


# --- FlowApp export -----------------------------------------------------------------
#
# The token stays in this process: it is looked up (and cached) here, handed to
# flowapp.Client and never put into a response. One send job runs at a time.

TOKEN_TTL = 300                 # seconds a looked-up token (or its absence) is reused
ME_TTL = 300                    # seconds FlowApp's /me answer is reused; a failure for ME_RETRY
ME_RETRY = 60
MAX_PREVIEW_DAYS = 93
PROBLEMS = {
    "no_url": "Set [flowapp] url in config.toml to turn the FlowApp export on.",
    "no_token": "No FlowApp token: set FLOWAPP_TOKEN, put it in the flowapp-token file next to config.toml "
                "(chmod 600) or, on macOS, in the Keychain as flowapp-api-token.",
    "token_file_mode": "The flowapp-token file can be read by others, so it was not read: chmod 600 it.",
    "unauthorized": "FlowApp refused the token: it may have expired or been revoked.",
    "unreachable": "FlowApp could not be reached; proposals are shown without its current entries.",
}
_flowapp_lock = threading.Lock()
_flowapp_cache = {}             # "token": (at, token, problem); ("me", url): (at, answer or ApiError);
                                # ("entries", url): (first, last, FlowApp's entries of those days)
_jobs = {}                      # job id -> SendJob, the latest few
_current_job = None
load_token = flowapp.load_token         # replaced in tests: they must never reach the real Keychain


def flowapp_reset():
    """Forget cached tokens, /me answers and jobs (tests, a changed token file)."""
    global _current_job
    with _flowapp_lock:
        _flowapp_cache.clear()
        _jobs.clear()
        _current_job = None


def _token():
    with _flowapp_lock:
        hit = _flowapp_cache.get("token")
        if hit and time.time() - hit[0] < TOKEN_TTL:
            return hit[1], hit[2]
    token, problem = load_token()
    with _flowapp_lock:
        _flowapp_cache["token"] = (time.time(), token, problem)
    return token, problem


def _known_token():
    """The token this process has looked up, if any - never a new lookup (no Keychain for an answer)."""
    with _flowapp_lock:
        hit = _flowapp_cache.get("token")
    return hit[1] if hit else None


def _flowapp_client(cfg):
    """(Client, None) when the export is on, else (None, problem code)."""
    if cfg.flowapp is None:
        return None, "no_url"
    token, problem = _token()
    if token is None:
        return None, problem
    return flowapp.Client(cfg.flowapp.url, token), None


def _me(client):
    key = ("me", client.url)
    with _flowapp_lock:
        hit = _flowapp_cache.get(key)
        if hit and time.time() - hit[0] < (ME_RETRY if isinstance(hit[1], flowapp.ApiError) else ME_TTL):
            answer = hit[1]
        else:
            answer = None
    if answer is None:
        try:
            answer = client.me()
        except flowapp.ApiError as e:
            answer = e
        with _flowapp_lock:
            _flowapp_cache[key] = (time.time(), answer)
    if isinstance(answer, flowapp.ApiError):
        raise answer
    return answer


def _pick(d, *names):
    for name in names:
        cur = d
        for part in name.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is not None:
            return cur
    return None


def flowapp_status(cfg):
    """Whether the export is on and for whom - never the token itself."""
    s = cfg.flowapp
    out = {"enabled": False, "problem": None, "message": None, "name": None, "expires_at": None, "scopes": None,
           "round_minutes": s.round_minutes if s else None, "gap_minutes": s.gap_minutes if s else None}
    client, problem = _flowapp_client(cfg)
    if client is None:
        return {**out, "problem": problem, "message": PROBLEMS[problem]}
    try:
        me = _me(client)
    except flowapp.ApiError as e:
        if e.status == 401:
            return {**out, "problem": "unauthorized", "message": e.message or PROBLEMS["unauthorized"]}
        return {**out, "enabled": True, "problem": "unreachable", "message": PROBLEMS["unreachable"]}
    me = me if isinstance(me, dict) else {}
    scopes = _pick(me, "scopes", "token.scopes", "data.scopes")
    return {**out, "enabled": True, "name": _pick(me, "name", "user.name", "data.name", "data.user.name"),
            "expires_at": _pick(me, "expiresAt", "expires_at", "token.expiresAt", "data.expiresAt",
                                "data.token.expiresAt"),
            "scopes": list(scopes) if isinstance(scopes, (list, tuple)) else None}


def _remote(client, first, last):
    """FlowApp's entries around the days (a day's UTC edges differ from local), or None.

    What came back is kept for the drafts that follow (see _known_remote).
    """
    lo, hi = first - timedelta(days=1), last + timedelta(days=1)
    try:
        entries = client.entries(lo, hi)
    except flowapp.ApiError:
        return None
    with _flowapp_lock:
        _flowapp_cache[("entries", client.url)] = (lo, hi, entries)
    return entries


def _known_remote(client, first, last):
    """FlowApp's entries for the days as the last preview found them, or None: nothing is asked.

    A draft changes only this tool's database, so it does not wait on FlowApp; the
    preview and the send ask it again. Forgotten when a send starts, as the job adds entries.
    """
    with _flowapp_lock:
        hit = _flowapp_cache.get(("entries", client.url))
    if hit and hit[0] <= first - timedelta(days=1) and last + timedelta(days=1) <= hit[1]:
        return hit[2]
    return None


def _days(query, now):
    today = datetime.fromtimestamp(now).date()
    first = today - timedelta(days=today.weekday() + 7)     # last full week by default
    last = first + timedelta(days=6)
    try:
        if _one(query, "from"):
            first = datetime.strptime(_one(query, "from"), "%Y-%m-%d").date()
        if _one(query, "to"):
            last = datetime.strptime(_one(query, "to"), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("from and to are dates, YYYY-MM-DD") from None
    if last < first:
        first, last = last, first
    if (last - first).days >= MAX_PREVIEW_DAYS:
        raise ValueError(f"at most {MAX_PREVIEW_DAYS} days at once")
    return first, last, today


def _view(conn, cfg, client, first, last, today, now, billable_only=True, ask=True):
    """(blocks, reconciled entries, FlowApp's entries known) for the days first..last.

    With ask=False FlowApp is not asked: what the last preview learnt is used.
    """
    blocks = flowapp.collect_blocks(conn, cfg, first, last, billable_only)
    remote = _remote(client, first, last) if ask else _known_remote(client, first, last)
    items = flowapp.reconcile(blocks, flowapp.load_exports(conn, first, last), remote, today, now)
    return blocks, items, remote is not None


def flowapp_preview(conn, cfg, client, query, now=None):
    now = now or time.time()
    first, last, today = _days(query, now)
    billable = _one(query, "billable") not in ("0", "false", "no")
    _, items, remote_ok = _view(conn, cfg, client, first, last, today, now, billable)
    return {"from": first.isoformat(), "to": last.isoformat(), "today": today.isoformat(), "remote_ok": remote_ok,
            "days": flowapp.days_json(items, flowapp.titles_of(conn))}


def _as_block(it, blocks, exports):
    """The Block behind a reconciled entry: the stored row's, or the proposal's."""
    row = exports.get(it["external_id"])
    if row is not None:
        return flowapp.block_from_row(row)
    return next((b for b in blocks if b.external_id == it["external_id"]), None)


def flowapp_draft(conn, cfg, client, body, now=None):
    """Apply one change from the page; the task it belongs to, as the preview shows it."""
    now = now or time.time()
    day, ext, action = (str(body.get(k) or "").strip() for k in ("day", "external_id", "action"))
    try:
        d = date_of(day)
    except ValueError:
        raise ValueError("day is a date, YYYY-MM-DD") from None
    today = datetime.fromtimestamp(now).date()
    # Only sending a deleted entry again needs FlowApp's word that it is gone.
    ask = action == "resend"
    blocks, items, _ = _view(conn, cfg, client, d, d, today, now, billable_only=False, ask=ask)
    it = next((x for x in items if x["external_id"] == ext), None)
    if it is None:
        raise LookupError(f"no entry {ext} on {day}")
    exports = flowapp.load_exports(conn, d, d)
    proposal = next((b for b in blocks if b.external_id == ext), None)
    fields = {"external_id": ext}
    if action == "edit":
        for name in ("start", "stop"):
            if name in body:
                fields[name] = flowapp.parse_edit_time(body[name], day)
        for name in ("comment", "redmine_id", "export_comment"):
            if name in body:
                fields[name] = body[name]
    elif action == "merge_next":
        same = sorted((x for x in items if x["key"] == it["key"] and x["status"] not in ("skipped",)),
                      key=lambda x: (x["start"], x["external_id"]))
        later = [x for x in same if (x["start"], x["external_id"]) > (it["start"], it["external_id"])]
        if not later:
            raise ValueError("there is no next block of this task that day")
        fields["next"] = _as_block(later[0], blocks, exports)
    elif action == "resend" and it["status"] != "deleted_remote":
        raise ValueError("only an entry deleted in FlowApp can be sent again")
    flowapp.save_draft(conn, proposal, action, fields, now)
    _, items, _ = _view(conn, cfg, client, d, d, today, now, billable_only=False, ask=False)
    mine = [x for x in items if x["key"] == it["key"] and x["client"] == it["client"]]
    return {"ok": True, "task": flowapp.task_json(it["key"], it["client"], flowapp.titles_of(conn).get(it["key"]),
                                                  mine)}


def date_of(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


def flowapp_send(conn, cfg, client, body, now=None):
    """Start the one send job for the selected entries: (status code, answer)."""
    global _current_job
    now = now or time.time()
    ids = body.get("external_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        return 400, {"error": "external_ids is a non-empty list"}
    with _flowapp_lock:
        if _current_job is not None and _current_job.snapshot()["running"]:
            return 409, {"error": "a send is already running", "job_id": _current_job.id}
    days = set()
    for ext in ids:
        row = conn.execute("SELECT day FROM flowapp_exports WHERE external_id = ?", (ext,)).fetchone()
        if row is not None:
            days.add(date_of(row["day"]))
        else:
            try:
                days.add(datetime.fromtimestamp(int(ext.rsplit(":", 1)[1])).date())
            except (IndexError, ValueError, OverflowError, OSError):
                pass
    if not days:
        return 400, {"error": "none of these entries is known"}
    first, last = min(days), max(days)
    today = datetime.fromtimestamp(now).date()
    blocks = flowapp.collect_blocks(conn, cfg, first, last, billable_only=False)
    remote = _remote(client, first, last)
    if remote is None:                  # what FlowApp holds is checked right before sending
        return 503, {"error": PROBLEMS["unreachable"]}
    items = flowapp.reconcile(blocks, flowapp.load_exports(conn, first, last), remote, today, now)
    send, rejected = flowapp.prepare_send(conn, items, ids, now)
    job = flowapp.SendJob(db.connect, client, send, rejected=rejected)
    with _flowapp_lock:
        if _current_job is not None and _current_job.snapshot()["running"]:
            return 409, {"error": "a send is already running", "job_id": _current_job.id}
        _flowapp_cache.pop(("entries", client.url), None)
        _current_job = job
        _jobs[job.id] = job
        for old in list(_jobs)[:-10]:
            del _jobs[old]
    job.start()
    return 200, {"job_id": job.id}


def flowapp_job(job_id):
    with _flowapp_lock:
        return _jobs.get(job_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "cc-statusline"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        # Every JSON answer passes here, so this is the one place the FlowApp token is cut out:
        # FlowApp may repeat it in an error message that ends up in a status, a job or a stored entry.
        data = body if isinstance(body, bytes) else \
            flowapp.redact(json.dumps(body, ensure_ascii=False), _known_token()).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/tasks":
            return self._send(200, TASKS_PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/flowapp":
            if not FLOWAPP_PAGE.exists():
                return self._send(404, {"error": "not found"})
            return self._send(200, FLOWAPP_PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path.startswith("/api/flowapp/"):
            return self._flowapp_get(url)
        if url.path in ("/api/tasks", "/api/task"):
            query = parse_qs(url.query)
            cfg = config.load()
            conn = db.connect()
            try:
                if url.path == "/api/tasks":
                    return self._send(200, build_tasks(conn, cfg, query))
                return self._send(200, build_task(conn, cfg, _one(query, "key")))
            except LookupError as e:
                return self._send(404, {"error": str(e)})
            finally:
                conn.close()
        if url.path == "/api/report":
            now = time.time()
            first, last, start, end = _range(parse_qs(url.query), now)
            cfg = config.load()
            conn = db.connect()
            try:
                data = build_report(conn, cfg, start, end)
            finally:
                conn.close()
            data["range"] = {"from": first.isoformat(), "to": last.isoformat()}
            data["settings"] = {"overlap": cfg.overlap, "idle_minutes": cfg.idle / 60,
                                "tool_cap_minutes": cfg.tool_cap / 60, "currency": cfg.currency,
                                "config_error": cfg.error, "config_path": str(paths.config_path())}
            return self._send(200, data)
        if url.path == "/api/session":
            query = parse_qs(url.query)
            cfg = config.load()
            conn = db.connect()
            try:
                sid = db.find_session(conn, query.get("id", [""])[0])
                start = end = None
                if query.get("from") and query.get("to"):
                    _, _, start, end = _range(query, time.time())
                return self._send(200, ledger.breakdown(conn, cfg, sid, start, end))
            except LookupError as e:
                return self._send(404, {"error": str(e)})
            finally:
                conn.close()
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-CC-Statusline") != "1":
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path not in ("/api/assign", "/api/task/status", "/api/task/move") + FLOWAPP_POSTS:
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, MAX_BODY)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "expected a JSON object"})
        if path in FLOWAPP_POSTS:
            return self._flowapp_post(path, body)
        if path != "/api/assign":
            return self._task_write(path, body)
        task = str(body.get("task") or "").strip() or None
        title = str(body.get("title") or "").strip() or None
        cfg = config.load()
        conn = db.connect()
        try:
            try:
                sid = db.find_session(conn, str(body.get("session_id") or ""))
            except LookupError as e:
                return self._send(404, {"error": str(e)})
            task = cfg.qualify(cfg.classify(db.session(conn, sid)["project_dir"])[0], task)
            now = time.time()
            effective = db.session(conn, sid)["first_ts"] if body.get("from_start", True) else now
            db.add_assignment(conn, sid, task, effective, "dashboard", now, branch=db.latest_branch(conn, sid))
            if task and title:
                db.set_task_title(conn, task, title, now)
            status.write(conn, cfg, sid, now)
        finally:
            conn.close()
        return self._send(200, {"ok": True, "session_id": sid, "task": task})


    def _flowapp_get(self, url):
        query = parse_qs(url.query)
        cfg = config.load()
        if url.path == "/api/flowapp/status":
            return self._send(200, flowapp_status(cfg))
        if url.path == "/api/flowapp/job":
            job = flowapp_job(_one(query, "id"))
            if job is None:
                return self._send(404, {"error": "no such job"})
            return self._send(200, job.snapshot())
        if url.path != "/api/flowapp/preview":
            return self._send(404, {"error": "not found"})
        client, problem = _flowapp_client(cfg)
        if client is None:
            return self._send(409, {"error": PROBLEMS[problem], "problem": problem})
        conn = db.connect()
        try:
            return self._send(200, flowapp_preview(conn, cfg, client, query))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        finally:
            conn.close()

    def _flowapp_post(self, path, body):
        if path == "/api/flowapp/job/cancel":
            job = flowapp_job(str(body.get("id") or ""))
            if job is None:
                return self._send(404, {"error": "no such job"})
            job.cancel()
            return self._send(200, {"ok": True})
        cfg = config.load()
        client, problem = _flowapp_client(cfg)
        if client is None:
            return self._send(409, {"error": PROBLEMS[problem], "problem": problem})
        conn = db.connect()
        try:
            if path == "/api/flowapp/draft":
                return self._send(200, flowapp_draft(conn, cfg, client, body))
            code, answer = flowapp_send(conn, cfg, client, body)
            return self._send(code, answer)
        except LookupError as e:
            return self._send(404, {"error": str(e).strip("'\"")})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        finally:
            conn.close()

    def _task_write(self, path, body):
        key = str(body.get("key") or "").strip()
        cfg = config.load()
        conn = db.connect()
        try:
            if path == "/api/task/status":
                worklist.set_status(conn, key, str(body.get("status") or "").strip(),
                                    str(body.get("outcome") or "").strip() or None, author="dashboard")
            else:
                key = worklist.move(conn, key, str(body.get("project") or "").strip(), author="dashboard")
            return self._send(200, {"ok": True, "task": build_task(conn, cfg, key)})
        except LookupError as e:
            return self._send(404, {"error": str(e).strip("'\"")})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        finally:
            conn.close()


def make_server(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def answers(port, timeout=1.0):
    """True when this dashboard, not just anything, serves on the port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/report?from=2000-01-01&to=2000-01-01",
                                    timeout=timeout) as r:
            return r.status == 200 and "totals" in json.loads(r.read())
    except Exception:
        return False


def serve(port=8765, open_browser=True, out=None, service=False):
    """Serve until Ctrl+C. A dashboard already running on the port is reused.

    As a login service a busy port ends it with status 0, so launchd and
    systemd, which restart it only after a failure, do not retry in a loop.
    """
    out = out or sys.stdout
    try:
        server = make_server(port)
    except OSError as e:
        if answers(port):
            print(f"cc-statusline dashboard already running: http://127.0.0.1:{port}/", file=out, flush=True)
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{port}/")
            return 0
        print(f"cc-statusline: cannot listen on 127.0.0.1:{port} ({e})", file=sys.stderr, flush=True)
        return 0 if service else 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"cc-statusline dashboard: {url}  (Ctrl+C to stop)", file=out, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
