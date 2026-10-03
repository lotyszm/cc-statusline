"""The work list: tasks as things to do, not only as labels for time.

A task lives in a project (a repository) and gets a number there, so it can be
called "#42" from inside that repository. Its key is "client:project#42", the
same key time is logged to. A ticket from Jira or Redmine is a field of the
task: a branch named after the ticket finds the task through it.

Every change is written to task_history, so "what changed and when" survives
without git. Writes go through here; the CLI and any front end only format.
"""

import json
import re
import subprocess
import time
from pathlib import Path

from . import db, ledger, tasktime

OPEN_STATUSES = db.OPEN_STATUSES
CLOSED_STATUSES = ("done", "parked")
STATUSES = {"task": OPEN_STATUSES + CLOSED_STATUSES, "decision": ("in-force", "revoked")}
# Statuses that close a record: they set closed_at, the others clear it.
CLOSING = ("done", "parked", "revoked")
# A task in progress whose record has not changed for this long needs a look.
STALE_DAYS = 7
# What a dump of the work list holds: everything but the raw time events.
DUMP_TABLES = ("projects", "tasks", "task_notes", "task_metrics", "task_history", "task_moves", "assignments")


# ── projects ─────────────────────────────────────────────────────────────────

def normalize_remote(url):
    """git@host:group/repo.git and https://host/group/repo.git -> host/group/repo."""
    if not url:
        return ""
    u = re.sub(r"\.git$", "", url.strip())
    u = re.sub(r"^[a-z]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    return u.replace(":", "/", 1)


def _git(cwd, *args):
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def repo_root(cwd):
    """The main checkout's root: a worktree belongs to the same project as its clone."""
    common = _git(cwd, "rev-parse", "--git-common-dir")
    if common:
        p = Path(common)
        if not p.is_absolute():
            p = Path(cwd) / p
        return str(p.parent.resolve())
    return str(Path(cwd).resolve())


def find_project(conn, cwd):
    """The project a directory belongs to, or None: by remote, then by path."""
    remote = normalize_remote(_git(cwd, "remote", "get-url", "origin"))
    if remote:
        row = conn.execute("SELECT * FROM projects WHERE remote = ?", (remote,)).fetchone()
        if row:
            return row
    root = repo_root(cwd)
    rows = [r for r in conn.execute("SELECT * FROM projects WHERE path IS NOT NULL")
            if root == r["path"] or root.startswith(r["path"].rstrip("/") + "/")]
    return max(rows, key=lambda r: len(r["path"])) if rows else None


def ensure_project(conn, cfg, cwd, now=None):
    """The project of cwd, created on first use with its client from the rules."""
    row = find_project(conn, cwd)
    if row:
        return row
    remote = normalize_remote(_git(cwd, "remote", "get-url", "origin"))
    root = repo_root(cwd)
    base = re.sub(r"[^a-z0-9]+", "-", (remote.split("/")[-1] if remote else Path(root).name).lower()).strip("-")
    slug, n = base or "project", 2
    while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    # Rules are written against the path as typed (/var/...), the root is resolved (/private/var/...).
    client = cfg.classify(str(cwd))[0] or cfg.classify(root)[0]
    conn.execute("INSERT INTO projects (slug, client, remote, path, created_at) VALUES (?, ?, ?, ?, ?)",
                 (slug, client, remote or None, root, now or time.time()))
    return conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()


def project(conn, slug):
    return conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()


def set_project(conn, slug, **fields):
    allowed = {"client", "remote", "path", "ticket_url", "repo_url"}
    given = {k: v for k, v in fields.items() if v is not None and k in allowed}
    if given:
        conn.execute(f"UPDATE projects SET {', '.join(f'{k} = ?' for k in given)} WHERE slug = ?",
                     [*given.values(), slug])


def key_for(proj, number):
    return f"{proj['client']}:{proj['slug']}#{number}" if proj["client"] else f"{proj['slug']}#{number}"


# ── finding tasks ────────────────────────────────────────────────────────────

def aliases(conn):
    return db.task_aliases(conn)


def resolve(conn, ref, proj=None):
    """A task key from '42', '#42', 'cms#42', a ticket or a full key; None if unknown."""
    ref = ref.strip()
    m = re.fullmatch(r"#?(\d+)", ref)
    moved = db.alias_of(db.task_aliases(conn), ref)
    if moved != ref and db.task_details(conn, moved) is not None and ":" in ref:
        return moved                     # an old key of a task moved to another project
    if m and proj is not None:
        row = conn.execute("SELECT task FROM tasks WHERE project = ? AND number = ?",
                           (proj["slug"], int(m.group(1)))).fetchone()
        return row["task"] if row else None
    m = re.fullmatch(r"([a-z0-9-]+)#(\d+)", ref)
    if m:
        row = conn.execute("SELECT task FROM tasks WHERE project = ? AND number = ?",
                           (m.group(1), int(m.group(2)))).fetchone()
        return row["task"] if row else None
    if db.task_details(conn, ref) is not None:
        return ref
    al = aliases(conn)
    client = proj["client"] if proj is not None else None
    return (al.get(f"{client}:{ref.upper()}") if client else None) or al.get(ref.upper())


# ── writing ──────────────────────────────────────────────────────────────────

def _history(conn, task, field, before, after, now, author):
    conn.execute("INSERT INTO task_history (task, ts, author, field, before, after) VALUES (?, ?, ?, ?, ?, ?)",
                 (task, now, author, field, None if before is None else str(before),
                  None if after is None else str(after)))


def add(conn, proj, title, now=None, author=None, kind="task", status="open", **fields):
    """A new task in a project; returns its key."""
    now = now or time.time()
    # The number is taken under the write lock: two adds at once would get the
    # same one, and the second would write over the first.
    conn.execute("BEGIN IMMEDIATE")
    try:
        number = conn.execute("SELECT coalesce(max(number), 0) + 1 FROM tasks WHERE project = ?",
                              (proj["slug"],)).fetchone()[0]
        key = key_for(proj, number)
        db.set_task(conn, key, now, title=title, kind=kind, status=status, project=proj["slug"],
                    number=number, created_at=now, **{k: v for k, v in fields.items() if v is not None})
        _history(conn, key, "created", None, title, now, author)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return key


def update(conn, key, now=None, author=None, **fields):
    """Change fields of a task, recording each change. Lists (pitfalls, commits...) are appended to."""
    now = now or time.time()
    row = db.task_details(conn, key)
    if row is None:
        raise LookupError(f"no task {key}")
    changed = {}
    conn.execute("BEGIN")
    try:
        for field, value in fields.items():
            if value is None:
                continue
            if field in db.TASK_LISTS:
                items = json.loads(row[field] or "[]")
                new = value if isinstance(value, list) else [value]
                items += [v for v in new if v not in items]
                changed[field] = json.dumps(items, ensure_ascii=False)
                for v in new:
                    _history(conn, key, field, None, v, now, author)
                continue
            if row[field] == value:
                continue
            changed[field] = value
            if field not in ("closed_at", "created_at", "number"):     # implied by status / creation
                _history(conn, key, field, row[field], value, now, author)
        if changed:
            db.set_task(conn, key, now, **changed)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return changed


def note(conn, key, text, now=None, author=None):
    conn.execute("INSERT INTO task_notes (task, ts, author, text) VALUES (?, ?, ?, ?)",
                 (key, now or time.time(), author, text))


def metric(conn, key, name, before, after, method, now=None):
    if not method:
        raise ValueError("a metric needs the method it was measured with")
    conn.execute("INSERT INTO task_metrics (task, ts, name, before, after, method) VALUES (?, ?, ?, ?, ?, ?)",
                 (key, now or time.time(), name, before, after, method))


def close(conn, key, status, outcome=None, now=None, author=None, **fields):
    """Close a task: an outcome is required, so history is more than a list of titles."""
    row = db.task_details(conn, key)
    if row is None:
        raise LookupError(f"no task {key}")
    if not (outcome or row["outcome"]):
        raise ValueError("closing needs an outcome: what was actually done")
    now = now or time.time()
    fields.pop("status", None)
    return update(conn, key, now, author, status=status, outcome=outcome, closed_at=now, **fields)


def set_status(conn, key, status, outcome=None, now=None, author=None):
    """Change a record's status the way the CLI would: done needs an outcome,
    a closing status stamps closed_at, reopening clears it."""
    row = db.task_details(conn, key)
    if row is None:
        raise LookupError(f"no task {key}")
    kind = row["kind"] or "task"
    if status not in STATUSES.get(kind, ()):
        raise ValueError(f"{status!r} is not a status of a {kind}; use one of {', '.join(STATUSES[kind])}")
    now = now or time.time()
    if status == "done":
        return close(conn, key, status, outcome or None, now, author)
    changed = update(conn, key, now, author, status=status, outcome=outcome or None,
                     closed_at=now if status in CLOSING and row["closed_at"] is None else None)
    if status not in CLOSING and row["closed_at"] is not None:
        conn.execute("UPDATE tasks SET closed_at = NULL, updated_at = ? WHERE task = ?", (now, key))
        changed["closed_at"] = None
    return changed


# Tables whose rows hang on a task key; a moved task takes them along.
KEYED_TABLES = ("assignments", "task_notes", "task_metrics", "task_history")


def move(conn, key, slug, now=None, author=None):
    """Move a task to another project, and so to that project's client.

    It gets the next number there and a key to match. Its sessions, notes,
    metrics, history and the dependencies other tasks list on it follow it, in
    one transaction, so its time is the same before and after. The old key
    keeps resolving to the new one. Returns the new key.
    """
    row = db.task_details(conn, key)
    if row is None:
        raise LookupError(f"no task {key}")
    target = project(conn, slug)
    if target is None:
        raise LookupError(f"no project {slug}")
    if row["project"] == slug:
        return key
    now = now or time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        number = conn.execute("SELECT coalesce(max(number), 0) + 1 FROM tasks WHERE project = ?",
                              (slug,)).fetchone()[0]
        new = key_for(target, number)
        if db.task_details(conn, new) is not None:
            raise ValueError(f"{new} already exists")
        conn.execute("UPDATE tasks SET task = ?, project = ?, number = ?, updated_at = ? WHERE task = ?",
                     (new, slug, number, now, key))
        for table in KEYED_TABLES:
            conn.execute(f"UPDATE {table} SET task = ? WHERE task = ?", (new, key))
        for other in conn.execute("SELECT task, depends_on FROM tasks WHERE depends_on LIKE ?",
                                  (f"%{key}%",)).fetchall():
            deps = [new if d == key else d for d in items(other["depends_on"])]
            conn.execute("UPDATE tasks SET depends_on = ? WHERE task = ?",
                         (json.dumps(deps, ensure_ascii=False), other["task"]))
        conn.execute("UPDATE task_moves SET new_task = ? WHERE new_task = ?", (new, key))
        conn.execute("INSERT OR REPLACE INTO task_moves (old_task, new_task, ts) VALUES (?, ?, ?)", (key, new, now))
        _history(conn, new, "moved", key, new, now, author)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return new


def assign_session(conn, cfg, session_id, key, now=None, since=None):
    """Log a session's time to a task, from `since`; by default from the start
    of the session when it has no task yet, else from now. Returns the moment."""
    now = now or time.time()
    sess = db.session(conn, session_id)
    if sess is None:
        raise LookupError(f"no session {session_id}")
    if since is None:
        cur = ledger.current(conn, cfg, session_id, now)
        fresh = not db.assignments(conn, [session_id])[session_id] and (cur is None or cur.task is None)
        since = sess["first_ts"] if fresh else now
    db.add_assignment(conn, session_id, key, since, "worklist", now, branch=db.latest_branch(conn, session_id))
    return since


# ── reading ──────────────────────────────────────────────────────────────────

def tasks(conn, project=None, statuses=None, kind=None, area=None, priority=None, closed_since=None):
    """Rows of the work list, filtered in SQL."""
    where, args = ["t.number IS NOT NULL"], []
    if project:
        where.append("t.project = ?")
        args.append(project)
    if kind:
        where.append("coalesce(t.kind, 'task') = ?")
        args.append(kind)
    if area:
        where.append("t.area = ?")
        args.append(area)
    if priority:
        where.append("t.priority = ?")
        args.append(priority)
    if statuses and closed_since is not None:
        where.append(f"(t.status IN ({', '.join('?' * len(statuses))}) OR t.closed_at >= ?)")
        args += [*statuses, closed_since]
    elif statuses:
        where.append(f"t.status IN ({', '.join('?' * len(statuses))})")
        args += list(statuses)
    return conn.execute(f"SELECT t.* FROM tasks t WHERE {' AND '.join(where)} ORDER BY t.project, t.number",
                        args).fetchall()


def seconds_per_task(conn, cfg, end=None):
    """Counted seconds per task key since the first event, as reports count them."""
    out = {}
    for (task, _sid), secs in tasktime.seconds(conn, cfg, end).items():
        out[task] = out.get(task, 0.0) + secs
    return out


def time_of(conn, cfg, key, end=None):
    """(counted seconds, [session rows]) of one task since the first event, as reports count them.

    Sessions come from the counted time, not from assignments only: a branch
    named after the ticket, or an assignment to the ticket key, counts too.
    """
    secs, sids = 0.0, set()
    for (task, sid), s in tasktime.seconds(conn, cfg, end).items():
        if task == key:
            secs += s
            sids.add(sid)
    rows = []
    ids = sorted(sids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows += conn.execute(f"SELECT session_id, first_ts, title FROM sessions "
                             f"WHERE session_id IN ({', '.join('?' * len(chunk))})", chunk).fetchall()
    return secs, sorted(rows, key=lambda r: r["first_ts"] or 0)


def ticket_url(conn, row):
    """Where a task's ticket lives: its own url, else the project's ticket address."""
    if row["url"]:
        return row["url"]
    proj = project(conn, row["project"]) if row["project"] else None
    if row["ticket"] and proj is not None and proj["ticket_url"]:
        return proj["ticket_url"].replace("{ticket}", row["ticket"])
    return None


def items(value):
    """A JSON list field as a list; text that is not JSON as one item."""
    try:
        out = json.loads(value) if value else []
    except ValueError:
        return [value]
    return out if isinstance(out, list) else [out]


def notes(conn, key):
    return conn.execute("SELECT * FROM task_notes WHERE task = ? ORDER BY ts, id", (key,)).fetchall()


def metrics(conn, key):
    return conn.execute("SELECT * FROM task_metrics WHERE task = ? ORDER BY ts, id", (key,)).fetchall()


def history(conn, key=None, since=None):
    where, args = [], []
    if key:
        where.append("task = ?")
        args.append(key)
    if since is not None:
        where.append("ts >= ?")
        args.append(since)
    sql = "SELECT * FROM task_history" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY ts, id"
    return conn.execute(sql, args).fetchall()


# ── upkeep ───────────────────────────────────────────────────────────────────

def doctor(conn, project=None, now=None):
    """What needs a move: (label, rows) groups, in the order they should be read."""
    now = now or time.time()
    rows = tasks(conn, project, OPEN_STATUSES, kind="task")
    going = [r for r in rows if r["status"] == "in-progress"]
    return [
        ("in progress without a next step", [r for r in going if not r["next_step"]]),
        (f"in progress, untouched for over {STALE_DAYS} days",
         [r for r in going if r["next_step"] and now - (r["updated_at"] or 0) > STALE_DAYS * 86400]),
        ("waiting for a decision", [r for r in rows if r["status"] == "waiting"]),
    ]


def dump(conn, out):
    """The work list as SQL that recreates it in an empty database, rows in a stable order."""
    out.write("BEGIN;\n")
    for table in DUMP_TABLES:
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        if ddl is None:
            continue
        out.write(ddl[0].replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1) + ";\n")
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            values = ", ".join("NULL" if v is None else str(v) if isinstance(v, (int, float))
                               else "'" + str(v).replace("'", "''") + "'" for v in row)
            out.write(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({values});\n")
    out.write("COMMIT;\n")
