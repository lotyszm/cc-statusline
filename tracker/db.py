"""SQLite storage. Raw events are the source of truth; everything else is derived.

Many hook processes write at once, so the database runs in WAL mode with a busy
timeout, and every write is a single short statement.
"""

import sqlite3
import time
from pathlib import Path

from . import paths

SCHEMA_VERSION = 2              # 2: assignments.branch

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
    task       TEXT PRIMARY KEY,
    title      TEXT,
    updated_at REAL
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


def _migrate(conn):
    """Bring tables created by older versions up to the current schema."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(assignments)")}
    if "branch" not in cols:
        try:
            conn.execute("ALTER TABLE assignments ADD COLUMN branch TEXT")
        except sqlite3.OperationalError:
            pass                        # another hook process migrated it first


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
    conn.execute("INSERT OR REPLACE INTO tasks (task, title, updated_at) VALUES (?, ?, ?)",
                 (task, title, now or time.time()))


def task_titles(conn):
    return {r["task"]: r["title"] for r in conn.execute("SELECT task, title FROM tasks")}


def mark_asked(conn, session_id, candidate, now=None):
    """Remember that the agent was told to ask about a task. True the first time."""
    cur = conn.execute("INSERT OR IGNORE INTO asked (session_id, candidate, ts) VALUES (?, ?, ?)",
                       (session_id, candidate, now or time.time()))
    return cur.rowcount == 1
