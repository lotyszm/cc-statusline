"""Claude Code hook handler: records each event and talks to the agent when useful.

statusline.py hands every hook event to this module. It has to be quick and it
must never break a session, so any failure ends with exit status 0 and a line in hook.log.
"""

import json
import math
import os
import re
import shlex
import shutil
import sys
import time
import traceback
from pathlib import Path

from . import config, db, gitinfo, ledger, paths, status, worklist

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


NEXT_MAX = 400


def _task_brief(conn, task):
    """What the agent should know about the current work-list task, or ''."""
    row = db.task_details(conn, task) if task else None
    if row is None or row["number"] is None:
        return ""
    lines = [f"Task status: {row['status'] or '-'}."]
    for name, label in (("next_step", "Next step"), ("criteria", "Done when")):
        if row[name]:
            text = " ".join(row[name].split())
            lines.append(f"{label}: {text[:NEXT_MAX]}{'…' if len(text) > NEXT_MAX else ''}")
    try:
        pitfalls = json.loads(row["pitfalls"] or "[]")
    except ValueError:
        pitfalls = []
    if pitfalls:
        lines.append(f"Known pitfalls: {len(pitfalls)}, see `{cli_command()} tasks show {task}`.")
    return "\n".join(lines) + "\n"


def _has_worklist(conn):
    return conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone() is not None


def _worklist_hint(cli):
    return (f"Work list: `{cli} tasks list --open --project .` shows this repo's open tasks, "
            f"`{cli} tasks show <TASK>` one in full; `tasks start|note|set|done` keep it current "
            "(set --next-step when you stop mid-way).\n")


def _session_context(info, session_id, brief="", worklist=False):
    task = info["task"] or "none"
    if info["task"] and info.get("title"):
        task += f" ({info['title']})"
    project = Path(info["project"]).name if info.get("project") else "-"
    cli = cli_command()
    return (
        f"cc-statusline: time in this session is tracked for billing (client {info['client']}, "
        f"project {project}, branch {info.get('branch') or '-'}). Current task: {task}.\n"
        + brief + (_worklist_hint(cli) if worklist else "") +
        "If the user says which task or ticket this work is for (an ID such as 12345 or PROJ-123, "
        "or a clear description) and it is not the current task, ask once, at the end of your "
        "reply, whether to log this session's time to it. A direct statement from the user counts "
        "as confirmation. After confirmation run:\n"
        f"  {cli_command()} task set <TASK-ID> --session {session_id} --title \"<short task title>\"\n"
        "Do not ask which task to log time to otherwise, and never ask twice about the same task in one session."
    )


def _prompt_context(conn, cfg, session_id, prompt, now):
    cur = ledger.current(conn, cfg, session_id, now)
    if cur is None or not cur.billable:
        return None
    aliases = db.task_aliases(conn)
    fresh = [db.alias_of(aliases, cfg.qualify(cur.client, c)) for c in cfg.prompt_candidates(prompt)]
    fresh = list(dict.fromkeys(fresh))
    fresh = [c for c in fresh if c != cur.task]
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


def _reminder(conn, cfg, session_id, now):
    """A line on the work list with each prompt, so the agent keeps it without being told."""
    if not cfg.remind or not _has_worklist(conn):
        return None
    cli = cli_command()
    cur = ledger.current(conn, cfg, session_id, now)
    row = db.task_details(conn, cur.task) if cur and cur.task else None
    if row is not None and row["number"] is not None and row["status"] in db.OPEN_STATUSES:
        title = f" ({_first_line(row['title'])})" if row["title"] else ""
        state = "" if row["status"] == "in-progress" else f", {row['status']}"
        return (f"cc-statusline work list: this session works on {cur.task}{title}{state}. Keep its next "
                f"step current and close it with `{cli} tasks done {cur.task} --outcome \"...\"` once "
                "verified; a different goal gets its own task.")
    return ("cc-statusline work list: no task is in progress in this session. Before changing anything, "
            f"take an open one (`{cli} tasks list --open --project .`, then `tasks start N`) or `tasks add` "
            "one; questions alone need none.")


# A work-list key as the coordinator writes it when it starts an agent: d24:cms#144 or cms#144.
TASK_KEY = re.compile(r"(?<![\w:#/-])((?:[a-z0-9_-]+:)?[a-z0-9-]+#\d+)(?![\w#])")
PROMPT_BYTES = 1024 * 1024          # the first transcript line holds the whole prompt


def _agent_files(transcript_path, agent_id):
    """The subagent's .meta.json and transcript, next to the session's transcript."""
    if not transcript_path or not agent_id or "/" in agent_id:
        return None
    p = Path(transcript_path)
    where = p.parent if p.parent.name == "subagents" else p.with_suffix("") / "subagents"
    return where / f"agent-{agent_id}.meta.json", where / f"agent-{agent_id}.jsonl"


def _agent_description(meta):
    """The description the subagent was started with: '' when it has none, None while unwritten."""
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (data.get("description") or "") if isinstance(data, dict) else ""


def _agent_prompt(transcript):
    """The subagent's prompt: '' when unreadable as text, None while unwritten."""
    try:
        with open(transcript, "rb") as f:
            message = json.loads(f.readline(PROMPT_BYTES)).get("message") or {}
    except (OSError, ValueError, AttributeError):
        return None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "\n".join(c.get("text") or "" for c in content if isinstance(c, dict))
    return content if isinstance(content, str) else ""


def _known(conn, text):
    found = []
    for ref in TASK_KEY.findall(text or ""):
        key = worklist.resolve(conn, ref)
        if key and key not in found:
            found.append(key)
    return found


def _only(keys):
    return keys[0] if len(keys) == 1 else None


def description_task(conn, description):
    """The task a description names, when it names exactly one known task.

    "Not #1, only #2" or "a regression after #1 breaks #2" name two: neither
    the first nor the last is reliably the agent's.
    """
    return _only(_known(conn, description))


def prompt_task(conn, prompt):
    """The task the prompt's first line names, when it names exactly one known task.

    Further down a prompt mentions related tasks and examples, which once
    pinned an agent to a key quoted as an example.
    """
    return _only(_known(conn, next((line for line in (prompt or "").splitlines() if line.strip()), None)))


def agent_task(conn, description, prompt):
    """The work-list task a subagent was started for, or None: the description's, else the prompt's."""
    return description_task(conn, description) or prompt_task(conn, prompt)


AUTO_ORIGINS = ("agent-prompt", "agent-description")


def _pin_agent(conn, session_id, agent_id, transcript_path, now):
    """Log a new subagent's time to the task its description or prompt names.

    Tried on the subagent's start and on its tool calls, since Claude Code may
    write the agent's files only after the start hook ran. The prompt alone
    may pin the agent, but only the description settles it: until the
    description is written it is looked for again, and when it names a task
    that task wins. A pin by hand is never replaced.

    The task the agent is pinned to by now, or None.
    """
    final, prompted = f"agent:{agent_id}", f"agent-prompt:{agent_id}"
    asked = {r[0] for r in conn.execute("SELECT candidate FROM asked WHERE session_id = ? AND candidate IN (?, ?)",
                                        (session_id, final, prompted))}
    if final in asked:
        return None
    rows = conn.execute("SELECT task, origin FROM assignments WHERE session_id = ? AND agent_id = ? "
                        "ORDER BY effective_from, id", (session_id, agent_id)).fetchall()
    if any(r["origin"] not in AUTO_ORIGINS for r in rows):
        db.mark_asked(conn, session_id, final, now)     # pinned by hand
        return None
    pinned = rows[-1]["task"] if rows else None
    files = _agent_files(transcript_path, agent_id)
    if files is None:
        return None
    meta, transcript = files

    def pin(task, origin):
        if task is not None and task != pinned:
            db.add_assignment(conn, session_id, task, db.agent_first_ts(conn, session_id, agent_id) or now,
                              origin, now, agent_id=agent_id)
        return task or pinned

    description = _agent_description(meta)
    task = description_task(conn, description)
    if task is not None:
        db.mark_asked(conn, session_id, final, now)
        return pin(task, "agent-description")
    if prompted not in asked:
        prompt = _agent_prompt(transcript)
        if prompt is None:
            return pinned                               # not written yet: look again on the next call
        db.mark_asked(conn, session_id, prompted, now)
        pinned = pin(prompt_task(conn, prompt), "agent-prompt")
    if description is None:
        return pinned                                   # the description may still name another task
    db.mark_asked(conn, session_id, final, now)
    return pinned


def _agent_context(conn, cfg, task):
    # In split mode an agent's own task does not count: its time stays with the session's.
    if not task or cfg.overlap != "full":
        return None
    row = db.task_details(conn, task)
    title = f" ({_first_line(row['title'])})" if row is not None and row["title"] else ""
    return f"cc-statusline: this agent's time is logged to {task}{title}."


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

    agent_id = payload.get("agent_id")
    if agent_id and event in ("SubagentStart", "PreToolUse"):
        pinned = _pin_agent(conn, session_id, agent_id, payload.get("transcript_path"), now)
        if event == "SubagentStart":
            return _output(event, _agent_context(conn, cfg, pinned))

    if event == "SessionStart":
        status.cleanup(now)
        info = status.write(conn, cfg, session_id, now)
        brief = _task_brief(conn, info["task"])
        if not info["billable"]:
            # Own projects are not billed, but a task picked up again still needs its next step.
            return _output(event, f"cc-statusline: current task {info['task']}"
                                  f"{' (' + info['title'] + ')' if info.get('title') else ''}.\n{brief}"
                                  + _worklist_hint(cli_command())) if brief else None
        return _output(event, _session_context(info, session_id, brief, _has_worklist(conn)))

    if event == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        title = _first_line(prompt)
        if title and not title.startswith(("/", "<")):
            db.set_session_title(conn, session_id, title, only_if_empty=True)
        ask = _prompt_context(conn, cfg, session_id, prompt, now)
        remind = _reminder(conn, cfg, session_id, now) if root else None
        return _output(event, "\n".join(filter(None, (ask, remind))))

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
