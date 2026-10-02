"""Local web dashboard: one page and a small JSON API, on 127.0.0.1 only.

Each request reads the config and the database afresh, so the page always
agrees with the CLI. Assigning a task is the only write. It needs a custom
header, which a page from another origin cannot send without a CORS preflight
this server never approves, and the Host check stops DNS rebinding.
"""

import json
import sys
import time
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db, ledger, paths, status

PAGE = Path(__file__).resolve().parent / "static" / "dashboard.html"
MAX_BODY = 64 * 1024


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


class Handler(BaseHTTPRequestHandler):
    server_version = "cc-statusline"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
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
        if urlparse(self.path).path != "/api/assign":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, MAX_BODY)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "expected a JSON object"})
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
