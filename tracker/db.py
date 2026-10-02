"""SQLite storage. Raw events are the source of truth; everything else is derived.

Many hook processes write at once, so the database runs in WAL mode with a busy
timeout, and every write is a single short statement.
"""

import sqlite3
import time
from pathlib import Path

from . import paths

SCHEMA_VERSION = 5              # 2: assignments.branch; 3: task details; 4: work list; 5: task moves

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    session_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    cwd         TEXT,
    project     TEXT,
    branch      TEXT,
    agent_id    TEXT,
    tool        TEXT,
    tool_use_id TEXT,
    source      TEXT NOT NULL DEFAULT 'hook',
    dedupe_key  TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, ts);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    agent       TEXT NOT NULL DEFAULT 'claude-code',
    account     TEXT,
    project_dir TEXT,
    first_ts    REAL,
    last_ts     REAL,
    title       TEXT
);

CREATE TABLE IF NOT EXISTS assignments (
    id             INTEGER PRIMARY KEY,
    session_id     TEXT NOT NULL,
    task           TEXT,               -- NULL: explicitly not a task
    effective_from REAL NOT NULL,
    created_at     REAL NOT NULL,
    origin         TEXT NOT NULL,
    branch         TEXT                -- branch the session was on when it was set
);
CREATE INDEX IF NOT EXISTS assignments_session ON assignments(session_id, effective_from);

CREATE TABLE IF NOT EXISTS tasks (
    task        TEXT PRIMARY KEY,
    title       TEXT,
    updated_at  REAL,
    description TEXT,               -- what the task is about
    plan        TEXT,               -- how it is to be solved, or how it was
    status      TEXT,               -- free text, as the tracker of origin names it
    url         TEXT                -- the ticket in Jira, Redmine and the like
);

CREATE TABLE IF NOT EXISTS projects (
    slug        TEXT PRIMARY KEY,    -- the "cms" in d24:cms#42
    client      TEXT,
    remote      TEXT UNIQUE,         -- normalised origin URL: host/group/repo
    path        TEXT,                -- repository root, when there is no remote
    ticket_url  TEXT,                -- prefix for ticket links, e.g. https://jira.example.com/browse/
    repo_url    TEXT,
    created_at  REAL
);

CREATE TABLE IF NOT EXISTS task_notes (
    id     INTEGER PRIMARY KEY,
    task   TEXT NOT NULL,
    ts     REAL NOT NULL,
    author TEXT,
    text   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_notes_task ON task_notes(task, ts);

CREATE TABLE IF NOT EXISTS task_metrics (
    id     INTEGER PRIMARY KEY,
    task   TEXT NOT NULL,
    ts     REAL,
    name   TEXT NOT NULL,
    before TEXT,
    after  TEXT,
    method TEXT NOT NULL             -- a number without how it was measured is a claim
);
CREATE INDEX IF NOT EXISTS task_metrics_task ON task_metrics(task);

CREATE TABLE IF NOT EXISTS task_history (
    id     INTEGER PRIMARY KEY,
    task   TEXT NOT NULL,
    ts     REAL NOT NULL,
    author TEXT,
    field  TEXT NOT NULL,
    before TEXT,
    after  TEXT
);
CREATE INDEX IF NOT EXISTS task_history_task ON task_history(task, ts);
CREATE INDEX IF NOT EXISTS task_history_ts ON task_history(ts);

CREATE VIEW IF NOT EXISTS v_tasks AS
    SELECT t.task, t.project, t.number, p.client, coalesce(t.kind, 'task') AS kind, t.status, t.priority,
           t.area, t.ticket, t.title, t.next_step, t.criteria, t.outcome,
           datetime(t.created_at, 'unixepoch', 'localtime') AS created,
           datetime(t.closed_at, 'unixepoch', 'localtime') AS closed,
           datetime(t.updated_at, 'unixepoch', 'localtime') AS updated
    FROM tasks t LEFT JOIN projects p ON p.slug = t.project;
-- A task moved to another project keeps answering to its old key.
CREATE TABLE IF NOT EXISTS task_moves (
    old_task TEXT PRIMARY KEY,
    new_task TEXT NOT NULL,
    ts       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS asked (
    session_id TEXT NOT NULL,
    candidate  TEXT NOT NULL,
    ts         REAL NOT NULL,
    PRIMARY KEY (session_id, candidate)
);

CREATE TABLE IF NOT EXISTS imported_files (
    path  TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size  INTEGER NOT NULL
);
"""

EVENT_FIELDS = ("ts", "session_id", "kind", "cwd", "project", "branch", "agent_id",
                "tool", "tool_use_id", "source", "dedupe_key")


def connect(path=None):
    path = Path(path or paths.db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    # Checked on every connect, not only on a version bump: the version alone
    # once said "current" while the column was missing (a hook raced an
    # upgrade), and checking costs a fraction of a millisecond.
    _migrate(conn)
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# Free-text fields of a task, settable one by one.
TASK_DETAILS = ("description", "plan", "status", "url",
                "kind", "project", "ticket", "priority", "area",
                "evidence", "next_step", "outcome", "tests", "criteria")
# JSON lists, appended to rather than replaced.
TASK_LISTS = ("pitfalls", "files", "commits", "depends_on")
TASK_COLUMNS = ([("assignments", "branch", "TEXT")]
                + [("tasks", c, "TEXT") for c in TASK_DETAILS + TASK_LISTS]
                + [("tasks", c, t) for c, t in (("number", "INTEGER"), ("created_at", "REAL"),
                                                ("closed_at", "REAL"))])


def _migrate(conn):
    """Bring tables created by older versions up to the current schema."""
    have = {}
    for table, column, kind in TASK_COLUMNS:
        if table not in have:
            have[table] = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column in have[table]:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError:
            pass                        # another hook process migrated it first
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS tasks_number ON tasks(project, number)")
    except sqlite3.OperationalError:
        pass


def record_event(conn, **ev):
    """Store one event; a repeated dedupe_key is ignored. True if stored."""
    ev.setdefault("source", "hook")
    cols = [f for f in EVENT_FIELDS if ev.get(f) is not None]
    cur = conn.execute(
        f"INSERT OR IGNORE INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [ev[c] for c in cols])
    return cur.rowcount == 1


def touch_session(conn, session_id, ts, account=None, project_dir=None, agent="claude-code"):
    # Insert, else widen the time span: no upsert, which SQLite before 3.24 lacks.
    cur = conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, agent, account, project_dir, first_ts, last_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, agent, account, project_dir, ts, ts))
    if cur.rowcount == 0:
        conn.execute(
            """UPDATE sessions SET
                   first_ts    = min(coalesce(first_ts, ?), ?),
                   last_ts     = max(coalesce(last_ts, ?), ?),
                   account     = coalesce(account, ?),
                   project_dir = coalesce(project_dir, ?)
               WHERE session_id = ?""",
            (ts, ts, ts, ts, account, project_dir, session_id))


def set_session_title(conn, session_id, title, only_if_empty=False):
    sql = "UPDATE sessions SET title = ? WHERE session_id = ?"
    if only_if_empty:
        sql += " AND (title IS NULL OR title = '')"
    conn.execute(sql, (title, session_id))


def session(conn, session_id):
    return conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()


def find_session(conn, prefix):
    """Full session id for a unique prefix; LookupError otherwise."""
    rows = conn.execute("SELECT session_id FROM sessions WHERE session_id LIKE ? LIMIT 2",
                        (prefix.replace("%", "") + "%",)).fetchall()
    if len(rows) != 1:
        raise LookupError(f"{'no' if not rows else 'more than one'} session matches '{prefix}'")
    return rows[0]["session_id"]


def add_assignment(conn, session_id, task, effective_from, origin, now=None, branch=None):
    conn.execute(
        "INSERT INTO assignments (session_id, task, effective_from, created_at, origin, branch) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, task, effective_from, now or time.time(), origin, branch))


def assignments(conn, session_ids):
    """{session_id: [(effective_from, task, branch)]} ordered by time, then by creation."""
    out = {sid: [] for sid in session_ids}
    ids = list(session_ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = conn.execute(
            f"SELECT session_id, effective_from, task, branch FROM assignments "
            f"WHERE session_id IN ({', '.join('?' * len(chunk))}) "
            f"ORDER BY session_id, effective_from, id", chunk)
        for r in rows:
            out[r["session_id"]].append((r["effective_from"], r["task"], r["branch"]))
    return out


def latest_branch(conn, session_id):
    row = conn.execute("SELECT branch FROM events WHERE session_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
                       (session_id,)).fetchone()
    return row["branch"] if row else None


def set_task_title(conn, task, title, now=None):
    set_task(conn, task, now, title=title)


def set_task(conn, task, now=None, **fields):
    """Store the given fields of a task, keeping the ones not given.

    No upsert and no INSERT OR REPLACE: the first needs SQLite 3.24, the second
    would wipe a description whenever a title is set.
    """
    unknown = set(fields) - {"title", "number", "created_at", "closed_at", *TASK_DETAILS, *TASK_LISTS}
    if unknown:
        raise ValueError(f"unknown task fields: {', '.join(sorted(unknown))}")
    now = now or time.time()
    conn.execute("INSERT OR IGNORE INTO tasks (task, updated_at) VALUES (?, ?)", (task, now))
    given = [k for k, v in fields.items() if v is not None]
    conn.execute(f"UPDATE tasks SET {''.join(f'{k} = ?, ' for k in given)}updated_at = ? WHERE task = ?",
                 [fields[k] for k in given] + [now, task])


def task_titles(conn):
    return {r["task"]: r["title"] for r in conn.execute("SELECT task, title FROM tasks")}


OPEN_STATUSES = ("open", "in-progress", "waiting")


def task_aliases(conn):
    """{"CLIENT:TICKET" or "TICKET": task key} for work-list tasks that carry a ticket.

    A branch or prompt names the ticket (CMS-706); the time belongs to the task
    that tracks it. Several records may share a ticket (a task and decisions
    taken on it): the alias goes to a task over a decision, an open one over a
    closed one, then the newest.
    """
    best = {}
    for r in conn.execute("SELECT t.task, t.ticket, t.kind, t.status, t.number, p.client FROM tasks t "
                          "LEFT JOIN projects p ON p.slug = t.project "
                          "WHERE t.ticket IS NOT NULL AND t.ticket != '' AND t.number IS NOT NULL"):
        score = ((r["kind"] or "task") == "task", r["status"] in OPEN_STATUSES, r["number"])
        ticket = r["ticket"].strip().upper()
        for alias in ([f"{r['client']}:{ticket}"] if r["client"] else []) + [ticket]:
            if alias not in best or score > best[alias][0]:
                best[alias] = (score, r["task"])
    out = {a: v[1] for a, v in best.items()}
    for r in conn.execute("SELECT old_task, new_task FROM task_moves"):
        out[_alias_form(r["old_task"])] = r["new_task"]
    return out


def project_clients(conn):
    """Clients the work list's projects belong to."""
    return {r[0] for r in conn.execute("SELECT DISTINCT client FROM projects WHERE client IS NOT NULL")}


def _alias_form(task):
    """The form alias_of looks a key up by: the part after the client upper-cased."""
    client, sep, rest = task.rpartition(":")
    return f"{client}{sep}{rest.upper()}"


def alias_of(aliases, task):
    """The work-list task a key stands for, or the key itself."""
    if not task or not aliases:
        return task
    return aliases.get(_alias_form(task), task)


def task_details(conn, task=None):
    """{task: row} for every task, or the row of one task (None if unknown)."""
    if task is not None:
        return conn.execute("SELECT * FROM tasks WHERE task = ?", (task,)).fetchone()
    return {r["task"]: r for r in conn.execute("SELECT * FROM tasks")}


def mark_asked(conn, session_id, candidate, now=None):
    """Remember that the agent was told to ask about a task. True the first time."""
    cur = conn.execute("INSERT OR IGNORE INTO asked (session_id, candidate, ts) VALUES (?, ?, ?)",
                       (session_id, candidate, now or time.time()))
    return cur.rowcount == 1
