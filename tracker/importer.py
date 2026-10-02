"""Backfill events from Claude Code transcripts.

Claude Code deletes transcripts after `cleanupPeriodDays` (30 by default), so
this is how history from before the hooks were installed gets in. Transcript
entries map onto the same event kinds the hooks record, so the same time rules
apply. Once the hooks have seen a session, its transcript is only used for the
time before their first event: from then on the hooks saw it first-hand, and
mixing the two sources would only blur the timing.

Importing is idempotent: every event carries the transcript entry's uuid as
its dedupe key, and files unchanged since the last run are not read again
unless `force` is set.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from . import db, gitinfo


def default_roots():
    """Every Claude Code account directory that has transcripts."""
    found = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        found.append(Path(env).expanduser())
    found += sorted(Path.home().glob(".claude*"))
    out = []
    for p in found:
        if (p / "projects").is_dir() and p not in out:
            out.append(p)
    return out


def _projects_dirs(root):
    """projects/ directories under an account dir, a projects dir, or a folder of accounts."""
    root = Path(root).expanduser()
    if root.name == "projects" and root.is_dir():
        return [root]
    if (root / "projects").is_dir():
        return [root / "projects"]
    try:
        return sorted(d / "projects" for d in root.iterdir() if (d / "projects").is_dir())
    except OSError:
        return []


def _files(projects):
    yield from sorted(projects.glob("*/*.jsonl"))
    # Subagents, including workflow agents nested under subagents/workflows/<id>/.
    yield from sorted(projects.glob("*/*/subagents/**/*.jsonl"))


def _ts(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def entry_events(rec):
    """Events for one transcript entry (user or assistant), without project info."""
    kind = rec.get("type")
    ts, sid = _ts(rec.get("timestamp")), rec.get("sessionId")
    if kind not in ("user", "assistant") or not ts or not sid:
        return []
    uid = rec.get("uuid") or f"{sid}:{rec.get('timestamp')}:{kind}"
    base = {"ts": ts, "session_id": sid, "cwd": rec.get("cwd"), "branch": rec.get("gitBranch"),
            "agent_id": rec.get("agentId") if rec.get("isSidechain") else None, "source": "import"}
    content = (rec.get("message") or {}).get("content")
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []

    if kind == "user":
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            return [dict(base, kind="tool_end", tool_use_id=b.get("tool_use_id"), dedupe_key=f"cc:{uid}:r{i}")
                    for i, b in enumerate(results)]
        return [dict(base, kind="prompt", dedupe_key=f"cc:{uid}")]

    uses = [b for b in blocks if b.get("type") == "tool_use"]
    if uses:
        return [dict(base, kind="tool_start", tool=b.get("name"), tool_use_id=b.get("id"),
                     dedupe_key=f"cc:{uid}:u{i}") for i, b in enumerate(uses)]
    return [dict(base, kind="agent", dedupe_key=f"cc:{uid}")]


def _parse(path):
    """(events, titles {sid: title}, project_dirs {sid: cwd of first main entry})."""
    events, titles, dirs = [], {}, {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # Cheap skip for snapshots and other bookkeeping lines.
            if '"user"' not in line and '"assistant"' not in line and '"aiTitle"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("type") == "ai-title":
                if rec.get("sessionId") and rec.get("aiTitle"):
                    titles[rec["sessionId"]] = rec["aiTitle"]
                continue
            evs = entry_events(rec)
            if evs and not rec.get("isSidechain") and rec.get("cwd"):
                dirs.setdefault(evs[0]["session_id"], rec["cwd"])
            events.extend(evs)
    return events, titles, dirs


def run(conn, roots, progress=None, force=False):
    stats = {"files": 0, "events": 0, "skipped_files": 0, "skipped_sessions": 0}
    # Per session, the moment the hooks took over.
    hooked = dict(conn.execute("SELECT session_id, min(ts) FROM events WHERE source = 'hook' GROUP BY session_id"))
    skipped, repos = set(), {}

    def project_of(cwd):
        if cwd not in repos:
            repos[cwd] = (gitinfo.repo_info(cwd)[0] if cwd and os.path.isdir(cwd) else None) or cwd
        return repos[cwd]

    for root in roots:
        for projects in _projects_dirs(root):
            account = projects.parent.name.lstrip(".") or None
            for path in _files(projects):
                st = path.stat()
                seen = conn.execute("SELECT mtime, size FROM imported_files WHERE path = ?",
                                    (str(path),)).fetchone()
                if not force and seen and seen["mtime"] == st.st_mtime and seen["size"] == st.st_size:
                    stats["skipped_files"] += 1
                    continue
                events, titles, dirs = _parse(path)
                conn.execute("BEGIN")
                try:
                    span = {}
                    for ev in events:
                        sid = ev["session_id"]
                        if sid in hooked and ev["ts"] >= hooked[sid]:
                            skipped.add(sid)
                            continue
                        ev["project"] = project_of(ev["cwd"])
                        if db.record_event(conn, **ev):
                            stats["events"] += 1
                        lo, hi = span.get(sid, (ev["ts"], ev["ts"]))
                        span[sid] = (min(lo, ev["ts"]), max(hi, ev["ts"]))
                    for sid, (lo, hi) in span.items():
                        for ts in (lo, hi):
                            db.touch_session(conn, sid, ts, account=account, project_dir=dirs.get(sid))
                    for sid, title in titles.items():
                        db.set_session_title(conn, sid, title, only_if_empty=sid in hooked)
                    conn.execute("INSERT OR REPLACE INTO imported_files (path, mtime, size) VALUES (?, ?, ?)",
                                 (str(path), st.st_mtime, st.st_size))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                stats["files"] += 1
                if progress and stats["files"] % 50 == 0:
                    progress(f"  {stats['files']} files, {stats['events']} events…")
    stats["skipped_sessions"] = len(skipped)
    return stats
