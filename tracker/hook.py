"""Claude Code hook handler: records each event and talks to the agent when useful.

statusline.py hands every hook event to this module. It has to be quick and it
must never break a session, so any failure ends with exit status 0 and a line in hook.log.
"""

import json
import math
import os
import shlex
import shutil
import sys
import time
import traceback
from pathlib import Path

from . import config, db, gitinfo, ledger, paths, status

KINDS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "PreToolUse": "tool_start",
    "PostToolUse": "tool_end",
    "PostToolUseFailure": "tool_end",
    "Notification": "waiting",
    "PermissionRequest": "waiting",
    "Stop": "stop",
    "StopFailure": "stop",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "SessionEnd": "session_end",
}

# Notifications that mean the agent is blocked on you. The others are skipped:
# idle_prompt fires about a minute after every reply and would credit that
# minute before each break.
WAITING_NOTIFICATIONS = frozenset({"permission_prompt", "elicitation_dialog", "elicitation_url_dialog"})

TITLE_MAX = 80
TAIL_BYTES = 256 * 1024             # how far back to look for the generated title
STATUS_REFRESH = 60                 # during a long turn, refresh the status line this often
LOG_MAX = 1024 * 1024
SCRIPT = Path(__file__).resolve().parent.parent / "statusline.py"
COMMAND = "cc-statusline"


def cli_command():
    """How the agent should call the CLI: by name when it is on PATH."""
    if shutil.which(COMMAND):
        return COMMAND
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))}"


def _account(transcript_path):
    """'claude-mc' for ~/.claude-mc/projects/<project>/<session>.jsonl."""
    if not transcript_path:
        return None
    return Path(transcript_path).parent.parent.parent.name.lstrip(".") or None


def _first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:TITLE_MAX]
    return None


def _ai_title(transcript_path):
    """The latest title Claude Code generated for the session, if any."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            tail = f.read()
    except (OSError, TypeError):
        return None
    for line in reversed(tail.splitlines()):
        if b'"aiTitle"' in line:
            try:
                return json.loads(line).get("aiTitle") or None
            except ValueError:
                continue
    return None


def _output(event, context):
    if not context:
        return None
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def _session_context(info, session_id):
    task = info["task"] or "none"
    if info["task"] and info.get("title"):
        task += f" ({info['title']})"
    project = Path(info["project"]).name if info.get("project") else "-"
    return (
        f"cc-statusline: time in this session is tracked for billing (client {info['client']}, "
        f"project {project}, branch {info.get('branch') or '-'}). Current task: {task}.\n"
        "If the user says which task or ticket this work is for (an ID such as 12345 or PROJ-123, "
        "or a clear description) and it is not the current task, ask once, at the end of your "
        "reply, whether to log this session's time to it. A direct statement from the user counts "
        "as confirmation. After confirmation run:\n"
        f"  {cli_command()} task set <TASK-ID> --session {session_id} --title \"<short task title>\"\n"
        "Do not bring up tasks otherwise, and never ask twice about the same task in one session."
    )


def _prompt_context(conn, cfg, session_id, prompt, now):
    cur = ledger.current(conn, cfg, session_id, now)
    if cur is None or not cur.billable:
        return None
    fresh = [c for c in cfg.prompt_candidates(prompt) if c != cur.task]
    fresh = [c for c in fresh if db.mark_asked(conn, session_id, c, now)]
    if not fresh:
        return None
    # A session that already had a task switches from this prompt on. One that
    # never had any takes the task from its very start (the CLI's default).
    had_task = cur.task is not None or bool(db.assignments(conn, [session_id])[session_id])
    # Rounded down: a moment even a fraction after the prompt would leave the
    # time up to the next event with the old task.
    since = f" --since {math.floor(now * 1000) / 1000:.3f}" if had_task else ""
    ids = ", ".join(fresh)
    target = fresh[0] if len(fresh) == 1 else "<TASK-ID>"
    return (
        f"cc-statusline: the user's message mentions possible task IDs: {ids}. "
        f"Current task for this session: {cur.task or 'none'}.\n"
        "Unless the user already said so explicitly, ask once, at the end of your reply, whether "
        f"this work belongs to {ids if len(fresh) == 1 else 'one of ' + ids} and should be logged "
        "to it. If they confirm, run:\n"
        f"  {cli_command()} task set {target} --session {session_id}{since} --title \"<short task title>\"\n"
        "If they decline, do not ask again."
    )


def handle(payload, conn, cfg, now, env):
    """Record one hook event; return the JSON to print, or None."""
    session_id = payload.get("session_id")
    if not session_id:
        return None
    event = payload.get("hook_event_name") or ""
    if event == "Notification" and payload.get("notification_type") not in WAITING_NOTIFICATIONS:
        return None
    cwd = payload.get("cwd") or None
    project_dir = env.get("CLAUDE_PROJECT_DIR") or cwd
    root, branch = gitinfo.repo_info(cwd)
    if root is None and project_dir != cwd:
        root, _ = gitinfo.repo_info(project_dir)

    db.record_event(conn, ts=now, session_id=session_id, kind=KINDS.get(event, "other"),
                    cwd=cwd, project=root or project_dir, branch=branch,
                    agent_id=payload.get("agent_id"), tool=payload.get("tool_name"),
                    tool_use_id=payload.get("tool_use_id"))
    db.touch_session(conn, session_id, now, account=_account(payload.get("transcript_path")),
                     project_dir=project_dir)

    if event == "SessionStart":
        status.cleanup(now)
        info = status.write(conn, cfg, session_id, now)
        return _output(event, _session_context(info, session_id)) if info["billable"] else None

    if event == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        title = _first_line(prompt)
        if title and not title.startswith(("/", "<")):
            db.set_session_title(conn, session_id, title, only_if_empty=True)
        return _output(event, _prompt_context(conn, cfg, session_id, prompt, now))

    if event in ("Stop", "StopFailure"):
        title = _ai_title(payload.get("transcript_path"))
        if title:
            db.set_session_title(conn, session_id, title)
        status.write(conn, cfg, session_id, now)
    elif KINDS.get(event) == "tool_end" and status.age(session_id, now) >= STATUS_REFRESH:
        # A long autonomous turn has no Stop for a while; keep the line current.
        status.write(conn, cfg, session_id, now)
    return None


def log(message):
    try:
        p = paths.log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > LOG_MAX:
            p.replace(p.with_suffix(".log.1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message.rstrip()}\n")
    except OSError:
        pass


def main(stdin=sys.stdin, stdout=sys.stdout, now=None):
    now = now or time.time()
    try:
        payload = json.load(stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        cfg = config.load()
        if cfg.error and payload.get("hook_event_name") == "SessionStart":
            log(f"config ignored: {cfg.error}")
        conn = db.connect()
        try:
            out = handle(payload, conn, cfg, now, os.environ)
        finally:
            conn.close()
        if out:
            stdout.write(json.dumps(out))
    except Exception:
        log(traceback.format_exc())
    return 0
