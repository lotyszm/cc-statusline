#!/usr/bin/env python3
"""
Claude Code status line: context usage, session/project cost, 5h and 7d limits.

Author:  Maciej Łotysz
Repo:    https://github.com/lotyszm/cc-statusline
License: MIT

Single file, no dependencies beyond python3. Works across multiple Claude Code
accounts simultaneously. Token prices are fetched from the LiteLLM feed and
refreshed once a day in the background, so nothing needs scheduling.

Quick start:

    chmod +x statusline.py
    ./statusline.py --install

Then restart Claude Code. Run --help for the other commands.

Costs are API list-price estimates. On a subscription they are not what you pay;
they show what the same usage would have cost through the API.
"""

__author__ = "Maciej Łotysz"
__version__ = "1.0.0"

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ══════════════════════════════ CONFIGURATION ══════════════════════════════

LANG = "en"                     # "en" or "pl" — labels shown in the status line
LAYOUT = "gauges"               # "gauges": 4 lines, ctx/5h/7d aligned in one column
                                # "compact": 2 lines, gauges side by side
BAR_W = 10                      # progress bar width in characters
BAR_STYLE = "solid"             # "solid": bars drawn with background colour, which
                                # stays opaque on translucent terminals.
                                # "ascii": drawn with █ glyphs instead.
SHOW_GIT = True                 # show branch, read straight from .git/HEAD

# 256-colour palette. Higher index means lighter in the 232-255 greyscale ramp.
# Preview:  for i in $(seq 232 255); do printf "\033[48;5;${i}m %3d \033[0m" $i; done
C_LABEL, C_SEP, C_VALUE, C_MONEY = 245, 242, 252, 221
C_MODEL, C_PATH, C_GIT, C_MUTED = 81, 111, 176, 245
C_ACCOUNT = 209                 # account tag, shown when the dir is not ~/.claude
C_WARN = 214
BAR_OK, BAR_WARN, BAR_HOT = 77, 214, 203    # thresholds are 70% and 90%
BAR_TRACK = 238                 # empty part of the bar; raise it on light themes

PRICES_TTL = 24 * 3600          # how often to refresh the price feed
PRICES_WARN_AFTER = 4 * 86400   # warn in the status line after this long with no
                                # successful fetch, so a broken network is visible
REFRESH_LOCK_TTL = 900          # minimum gap between fetch attempts, failures included
RECENT_DAYS = 8                 # how much history to keep for the rolling windows
LIMITS_FRESH = 300              # how long a rate-limit reading taken by another
                                # session stays trustworthy, see sync_limits()

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Used when the feed has never been fetched or a model cannot be matched at all.
# USD per 1M tokens, (input, output); first matching substring wins.
FALLBACK_FAMILIES = [
    ("fable",    10.00, 50.00),
    ("opus-4-1", 15.00, 75.00),
    ("opus-4-2", 15.00, 75.00),
    ("opus",      5.00, 25.00),
    ("sonnet-5",  2.00, 10.00),
    ("sonnet",    3.00, 15.00),
    ("haiku-3-5", 0.80,  4.00),
    ("haiku",     1.00,  5.00),
]
FALLBACK_DEFAULT = (3.00, 15.00)
FB_READ, FB_W5M, FB_W1H = 0.10, 1.25, 2.00      # multipliers over the input price

LABELS = {
    "en": {"session": "session", "project": "project", "total": "all",
           "new_dir": "new directory", "no_limit": "limit n/a", "tok": "tok",
           "no_prices": "no price data", "stale": "prices {d}d old",
           "unpriced": "unpriced model"},
    "pl": {"session": "sesja", "project": "projekt", "total": "razem",
           "new_dir": "nowy katalog", "no_limit": "limit n/d", "tok": "tok",
           "no_prices": "brak cennika", "stale": "cennik {d}d",
           "unpriced": "model spoza cennika"},
}

# ══════════════════════════════ PATHS AND STATE ════════════════════════════

# The account directory is resolved per invocation, see detect_config_dir().
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
ACCOUNT = ""

# Caches live outside the account directories: prices are account independent
# and shared, the session scan is per account.
CACHE_ROOT = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "cc-statusline"
PRICES_FILE = CACHE_ROOT / "prices.json"
LOCK_FILE = CACHE_ROOT / "refresh.lock"
CACHE_FILE = CACHE_ROOT / "scan-claude.json"
LIMITS_FILE = CACHE_ROOT / "limits-claude.json"

PRICE_MAP = {}          # model -> [in, out, cache_write_5m, cache_read, cache_write_1h]
PRICES_AGE = None       # seconds since the last successful fetch, None if never
UNKNOWN = set()         # models that had to fall back to guessed rates


def L(key, **kw):
    return LABELS.get(LANG, LABELS["en"])[key].format(**kw)


def set_config_dir(path):
    global CLAUDE_DIR, PROJECTS_DIR, CACHE_FILE, LIMITS_FILE, ACCOUNT
    CLAUDE_DIR = Path(path).expanduser()
    PROJECTS_DIR = CLAUDE_DIR / "projects"
    name = CLAUDE_DIR.name.lstrip(".") or "claude"
    tag = hashlib.md5(str(CLAUDE_DIR).encode()).hexdigest()[:6]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name)
    CACHE_FILE = CACHE_ROOT / f"scan-{slug}-{tag}.json"
    LIMITS_FILE = CACHE_ROOT / f"limits-{slug}-{tag}.json"
    ACCOUNT = "" if name == "claude" else name


def detect_config_dir(data):
    """Derive the account directory from transcript_path in the stdin payload.

    The path is always <account>/projects/<project>/<session>.jsonl, which makes
    this reliable whether or not CLAUDE_CONFIG_DIR reaches the subprocess.
    """
    tp = (data or {}).get("transcript_path")
    if tp:
        cfg = Path(tp).parent.parent.parent
        if (cfg / "projects").is_dir():
            return cfg
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def find_accounts():
    """All plausible Claude Code account directories on this machine."""
    found = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        found.append(Path(env).expanduser())
    for p in sorted(Path.home().glob(".claude*")):
        # A freshly created account has settings.json before it has projects/.
        if p.is_dir() and ((p / "projects").is_dir()
                           or (p / "settings.json").exists()
                           or p.name == ".claude"):
            found.append(p)
    seen, out = set(), []
    for p in found:
        if p.is_dir() and str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


# ══════════════════════════════ PRICING ════════════════════════════════════

def fetch_prices():
    """Fetch the feed, keep the Anthropic rows, return (count, changes)."""
    import urllib.request

    req = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "cc-statusline"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = json.loads(r.read().decode("utf-8"))

    models = {}
    for key, v in raw.items():
        if not isinstance(v, dict) or v.get("litellm_provider") != "anthropic":
            continue
        i, o = v.get("input_cost_per_token"), v.get("output_cost_per_token")
        if i is None or o is None:
            continue
        # Per-model cache rates matter: they are not always a fixed multiple of
        # the input price (Fable 5.1 reads at $0.25/M, not 10% of $10).
        w5 = v.get("cache_creation_input_token_cost") or i * FB_W5M
        rd = v.get("cache_read_input_token_cost") or i * FB_READ
        w1 = v.get("cache_creation_input_token_cost_above_1hr") or i * FB_W1H
        models[key.split("/")[-1].lower()] = [round(x * 1e6, 6) for x in (i, o, w5, rd, w1)]

    if not models:
        raise RuntimeError("feed contains no anthropic models")

    try:
        prev = json.loads(PRICES_FILE.read_text(encoding="utf-8")).get("models") or {}
    except Exception:
        prev = {}
    changes = []
    for k in sorted(set(models) | set(prev)):
        a, b = prev.get(k), models.get(k)
        if a is None:
            changes.append(("+", k, None, b))
        elif b is None:
            changes.append(("-", k, a, None))
        elif [round(x, 4) for x in a] != [round(x, 4) for x in b]:
            changes.append(("~", k, a, b))

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = PRICES_FILE.with_suffix(".tmp%d" % os.getpid())
    tmp.write_text(json.dumps({"ts": int(time.time()), "models": models},
                              separators=(",", ":")), encoding="utf-8")
    tmp.replace(PRICES_FILE)
    return len(models), changes


def spawn_refresh():
    """Kick off a fetch in a detached process. Never blocks the status line."""
    try:
        if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < REFRESH_LOCK_TTL:
            return
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        LOCK_FILE.touch()
        subprocess.Popen(
            # realpath, so the command still resolves when this file is reached
            # through a symlink on PATH
            [sys.executable, os.path.realpath(__file__), "--refresh-prices", "--quiet"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:
        pass


def load_prices():
    global PRICE_MAP, PRICES_AGE
    stale = True
    try:
        blob = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
        PRICE_MAP = blob.get("models") or {}
        PRICES_AGE = time.time() - blob.get("ts", 0)
        stale = PRICES_AGE > PRICES_TTL
    except Exception:
        PRICE_MAP, PRICES_AGE = {}, None
    if stale:
        spawn_refresh()     # this render uses whatever is on disk already


def resolve(model):
    """Match a model id to rates: exact, then dateless, then longest prefix.

    The prefix step is what keeps a newly released model priced sensibly before
    the feed catches up: claude-opus-5-1-20261115 lands on claude-opus-5 rates.
    """
    m = (model or "").lower().split("/")[-1]
    if not m:
        return None
    if m in PRICE_MAP:
        return PRICE_MAP[m]
    m2 = re.sub(r"-\d{8}$", "", m)
    if m2 in PRICE_MAP:
        return PRICE_MAP[m2]
    best = None
    for k in PRICE_MAP:
        if (m.startswith(k) or k.startswith(m2)) and (best is None or len(k) > len(best)):
            best = k
    if best:
        return PRICE_MAP[best]
    UNKNOWN.add(m)
    for pat, pin, pout in FALLBACK_FAMILIES:
        if pat in m:
            return [pin, pout, pin * FB_W5M, pin * FB_READ, pin * FB_W1H]
    pin, pout = FALLBACK_DEFAULT
    return [pin, pout, pin * FB_W5M, pin * FB_READ, pin * FB_W1H]


def usage_cost(model, u):
    pin, pout, pw5, prd, pw1 = resolve(model)
    i = u.get("input_tokens") or 0
    o = u.get("output_tokens") or 0
    r = u.get("cache_read_input_tokens") or 0
    cc = u.get("cache_creation")
    if isinstance(cc, dict):
        w5 = cc.get("ephemeral_5m_input_tokens") or 0
        w1 = cc.get("ephemeral_1h_input_tokens") or 0
    else:
        w5 = u.get("cache_creation_input_tokens") or 0
        w1 = 0
    cost = (i * pin + o * pout + r * prd + w5 * pw5 + w1 * pw1) / 1_000_000.0
    return i + o + r + w5 + w1, cost


# ══════════════════════════════ SESSION SCAN ═══════════════════════════════

def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_file(path):
    """Aggregate one transcript: (tokens, cost, recent events, models seen)."""
    total_t = total_c = 0.0
    recent, models, seen = [], set(), set()
    cutoff = time.time() - RECENT_DAYS * 86400
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                model = msg.get("model") or ""
                if "synthetic" in model:
                    continue
                key = (msg.get("id"), rec.get("requestId"))
                if key != (None, None):
                    if key in seen:            # retries repeat the same usage block
                        continue
                    seen.add(key)
                t, c = usage_cost(model, u)
                if not t and not c:
                    continue
                models.add(model.lower())
                total_t += t
                total_c += c
                ts = parse_ts(rec.get("timestamp"))
                if ts and ts >= cutoff:
                    recent.append([int(ts), int(t), round(c, 6)])
    except OSError:
        pass
    return total_t, total_c, recent, sorted(models)


def price_sig():
    """Cache key component: a price change must invalidate computed costs."""
    payload = json.dumps([PRICE_MAP, FALLBACK_FAMILIES, FALLBACK_DEFAULT,
                          FB_READ, FB_W5M, FB_W1H], sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()[:10]


def scan():
    """Aggregate every transcript, reusing cached results for unchanged files."""
    sig = price_sig()
    try:
        blob = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        old = blob.get("files") or {} if blob.get("sig") == sig else {}
    except Exception:
        old = {}

    files = {}
    if PROJECTS_DIR.is_dir():
        for proj in PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            for p in proj.glob("*.jsonl"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                k = str(p)
                prev = old.get(k)
                if prev and prev.get("m") == int(st.st_mtime) and prev.get("s") == st.st_size:
                    files[k] = prev
                else:
                    t, c, r, mm = parse_file(p)
                    files[k] = {"m": int(st.st_mtime), "s": st.st_size, "p": proj.name,
                                "t": t, "c": c, "r": r, "mm": mm}

    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps({"sig": sig, "files": files}, separators=(",", ":")),
                       encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except OSError:
        pass
    return files


def sync_limits(fh, sd):
    """Share rate-limit readings between concurrent sessions on one account.

    rate_limits arrives with a session's own API responses, so a session that
    has been idle keeps reporting whatever it saw last, sometimes no window at
    all. The readings are account wide, so every session describes the same two
    windows and the one that spoke to the API most recently holds the truth.

    Two rules decide which reading wins. The window is named by its reset time,
    and that name is taken from this session whenever its own window is still
    running: adopting a foreign reset time lets one bad reading outlive every
    correct one, with nothing able to displace it until that time passes. Inside
    the window a stored percentage is preferred only while it is fresh, because
    usage grows but the limit itself can be raised mid-window, and after a boost
    an older percentage reads too high forever.

    Returns the windows plus a flag saying whether a value came from another
    session rather than this payload.
    """
    now = time.time()
    try:
        store = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
    except Exception:
        store = {}
    # A window that has already reset says nothing about the current one.
    store = {k: v for k, v in store.items()
             if isinstance(v, dict) and (v.get("resets_at") or 0) > now}

    out, borrowed, changed = {}, {}, False
    for key, win in (("five_hour", fh), ("seven_day", sd)):
        cur = dict(win)
        pv, ra = win.get("used_percentage"), win.get("resets_at")
        prev = store.get(key) or {}
        pra, ppv = prev.get("resets_at"), prev.get("used_percentage")
        live = ra is not None and ra > now
        fresh = ppv is not None and now - (prev.get("seen_at") or 0) <= LIMITS_FRESH
        # Reset times are reported as floats and need not be byte-identical
        # between two sessions' payloads, so allow a couple of minutes of drift.
        same_window = live and pra is not None and abs(pra - ra) <= 120

        if live:
            if same_window and fresh and (pv is None or ppv > pv):
                cur["used_percentage"] = ppv
                borrowed[key] = True
            elif pv is not None:
                # Ours is the reading to beat; publishing it also refreshes
                # seen_at, which is what keeps the entry usable for the others.
                store[key] = {"resets_at": ra, "used_percentage": pv,
                              "seen_at": int(now)}
                changed = True
        elif ppv is not None and pra is not None:
            # Nothing usable of our own: no window in the payload, or one that
            # has already reset. Adopt the stored window whole, resets_at
            # included, since that anchors the spend figures as well as the
            # gauge and the percentage alone would be scored against a window
            # that has ended.
            cur["used_percentage"], cur["resets_at"] = ppv, pra
            borrowed[key] = True
        elif ra is not None:
            cur["used_percentage"] = cur["resets_at"] = None
        out[key] = cur

    if changed:
        try:
            CACHE_ROOT.mkdir(parents=True, exist_ok=True)
            tmp = LIMITS_FILE.with_suffix(".tmp%d" % os.getpid())
            tmp.write_text(json.dumps(store, separators=(",", ":")), encoding="utf-8")
            tmp.replace(LIMITS_FILE)
        except OSError:
            pass
    return out["five_hour"], out["seven_day"], borrowed


def project_key(data):
    """Which directory under <account>/projects/ holds the current project."""
    tp = data.get("transcript_path")
    if tp:
        parent = Path(tp).parent
        if parent.is_dir():
            return parent.name
    cwd = (data.get("workspace") or {}).get("project_dir") or data.get("cwd") or ""
    for cand in (re.sub(r"[^a-zA-Z0-9]", "-", cwd), cwd.replace("/", "-")):
        if cand and (PROJECTS_DIR / cand).is_dir():
            return cand
    return None


def git_branch(start):
    """Read HEAD directly; spawning git would cost more than the whole render."""
    p = Path(start or ".")
    for _ in range(8):
        g = p / ".git"
        if g.is_file():                      # worktree or submodule pointer file
            try:
                txt = g.read_text(encoding="utf-8").strip()
                if txt.startswith("gitdir:"):
                    g = Path(txt.split(":", 1)[1].strip())
            except OSError:
                return None
        if g.is_dir():
            try:
                head = (g / "HEAD").read_text(encoding="utf-8").strip()
            except OSError:
                return None
            return head.split("refs/heads/", 1)[1] if "refs/heads/" in head else head[:7]
        if p.parent == p:
            break
        p = p.parent
    return None


# ══════════════════════════════ FORMATTING ═════════════════════════════════

R, B = "\033[0m", "\033[1m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def fg(idx, s):
    return f"\033[38;5;{idx}m{s}{R}"


def lbl(s):
    return fg(C_LABEL, s)


def val(s):
    return fg(C_VALUE, s)


def money(x):
    return fg(C_MONEY, x)


def vlen(s):
    return len(ANSI_RE.sub("", s))


def pad(s, w):
    return s + " " * max(0, w - vlen(s))


def bar(pct):
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct * BAR_W / 100.0))
    c = BAR_HOT if pct >= 90 else (BAR_WARN if pct >= 70 else BAR_OK)
    if BAR_STYLE == "solid":
        # Background colour is fully opaque, unlike the faint attribute, which
        # composites against the desktop on a translucent terminal.
        return (f"\033[48;5;{c}m{' ' * filled}"
                f"\033[48;5;{BAR_TRACK}m{' ' * (BAR_W - filled)}{R}")
    return (f"\033[38;5;{c}m{'█' * filled}"
            f"\033[38;5;{BAR_TRACK}m{'█' * (BAR_W - filled)}{R}")


def pct_txt(p, borrowed=False):
    # The trailing slot is always one character wide so columns stay aligned;
    # "~" marks a reading carried over from another session on this account.
    return val(f"{int(p or 0):>3}%") + fg(C_MUTED, "~" if borrowed else " ")


def tok(n):
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1_000:.0f}k" if n >= 1_000 else f"{int(n)}"


def cap(n):
    n = float(n or 0)
    if n >= 1_000_000:
        m = n / 1_000_000
        return f"{m:.0f}M" if abs(m - round(m)) < 0.05 else f"{m:.1f}M"
    return f"{n / 1000:.0f}k"


def usd(x):
    x = float(x or 0)
    return f"${x:,.2f}" if x < 1000 else f"${x:,.0f}"


def eta(ts):
    d = int((ts or 0) - time.time())
    if d <= 0:
        return "—"
    h, m = d // 3600, (d % 3600) // 60
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    return f"{h}h {m}m" if h else f"{m}m"


def short_path(p):
    s = str(p)
    home = str(Path.home())
    if s.startswith(home):
        s = "~" + s[len(home):]
    parts = s.split("/")
    return "/".join(parts[-2:]) if len(parts) > 3 else s


# ══════════════════════════════ RENDER ═════════════════════════════════════

def render(data):
    model = (data.get("model") or {}).get("display_name") or "?"
    model = re.sub(r"\s*\(.*?\)", "", model)      # context size is shown on the ctx row
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or os.getcwd()
    ctx = data.get("context_window") or {}
    effort = (data.get("effort") or {}).get("level")

    now = time.time()
    rl = data.get("rate_limits") or {}
    fh, sd, borrowed = sync_limits(rl.get("five_hour") or {}, rl.get("seven_day") or {})
    h5_reset, d7_reset = fh.get("resets_at"), sd.get("resets_at")
    # Anchor the windows to the reported reset time rather than counting back
    # from now, so the figures line up with how the limits are actually scored.
    h5_start = (h5_reset - 5 * 3600) if h5_reset else now - 5 * 3600
    d7_start = (d7_reset - 7 * 86400) if d7_reset else now - 7 * 86400

    files = scan()
    pkey = project_key(data)

    pt = pc = p5c = p7c = p5t = p7t = 0.0
    g5t = g5c = g7t = g7c = 0.0
    approx = False

    for e in files.values():
        mine = pkey is not None and e.get("p") == pkey
        if mine:
            pt += e.get("t", 0)
            pc += e.get("c", 0)
            for m in e.get("mm", ()):
                resolve(m)
                if m in UNKNOWN:
                    approx = True
        for ts, t, c in e.get("r", ()):
            if ts >= d7_start:
                g7t += t
                g7c += c
                if mine:
                    p7c += c
                    p7t += t
            if ts >= h5_start:
                g5t += t
                g5c += c
                if mine:
                    p5c += c
                    p5t += t

    st_t = st_c = 0.0
    tp = data.get("transcript_path")
    if tp and str(tp) in files:
        st_t = files[str(tp)].get("t", 0)
        st_c = files[str(tp)].get("c", 0)

    dur_s = int(((data.get("cost") or {}).get("total_duration_ms") or 0) / 1000)
    dur = f"{dur_s // 3600}h {(dur_s % 3600) // 60}m" if dur_s >= 3600 else f"{dur_s // 60}m"

    sep = f" {fg(C_SEP, chr(183))} "
    out = []

    head = []
    if ACCOUNT:
        head.append(fg(C_ACCOUNT, f"{B}[{ACCOUNT}]"))
    head.append(fg(C_MODEL, f"{B}{model}"))
    if effort:
        head.append(fg(C_MUTED, effort))
    head.append(fg(C_PATH, short_path(cwd)))
    br = git_branch(cwd) if SHOW_GIT else None
    if br:
        head.append(fg(C_GIT, f"⑂ {br}"))
    head.append(fg(C_MUTED, L("new_dir") if pkey is None
                  else f"{L('project')} {usd(pc)} / {tok(pt)}"))
    if PRICES_AGE is None:
        head.append(fg(C_WARN, f"⚠ {L('no_prices')}"))
    elif PRICES_AGE > PRICES_WARN_AFTER:
        head.append(fg(C_WARN, "⚠ " + L("stale", d=int(PRICES_AGE // 86400))))
    if approx:
        head.append(fg(C_WARN, f"⚠ {L('unpriced')}"))
    out.append(sep.join(head))

    pct = int(ctx.get("used_percentage") or 0)
    ctx_used = ctx.get("total_input_tokens") or 0     # matches used_percentage:
    ctx_max = ctx.get("context_window_size") or 0     # input + cache write + cache read
    ctx_txt = f"{tok(ctx_used)}/{cap(ctx_max)}" if ctx_max else f"{tok(ctx_used)} {L('tok')}"
    h5p, d7p = fh.get("used_percentage"), sd.get("used_percentage")
    WINDOW_KEY = {"5h": "five_hour", "7d": "seven_day"}

    def meter(name, pct_v):
        """Bar and percentage, or `limit n/a` padded to the same width."""
        if pct_v is None:
            return pad(fg(C_MUTED, L("no_limit")), BAR_W + 6)
        return f"{bar(pct_v)} {pct_txt(pct_v, borrowed.get(WINDOW_KEY.get(name)))}"

    if LAYOUT == "compact":
        out.append("   ".join([
            f"{lbl('ctx')} {bar(pct)} {pct_txt(pct)}{val(ctx_txt)}",
            f"{lbl('5h')} {meter('5h', h5p)}",
            f"{lbl('7d')} {meter('7d', d7p)}",
            f"{money(usd(st_c))} {fg(C_MUTED, L('session'))}"
            f"{sep}{money(usd(g7c))} {fg(C_MUTED, '7d')}",
        ]))
        return "\n".join(out)

    try:
        cols = int(os.environ.get("COLUMNS") or 0)
    except ValueError:
        cols = 0
    wide = cols == 0 or cols >= 95
    gw = 22 + BAR_W

    def gauge(name, pct_v, meta):
        return f"{lbl(f'{name:<4}')}{meter(name, pct_v)} {fg(C_MUTED, meta)}"

    def pair(label, cost, toks):
        # Cost and token count always belong to the same scope and stay
        # adjacent, so a number is never read against the wrong label.
        s = f"{lbl(label)} {money(usd(cost))}"
        return s + " " + fg(C_MUTED, f"/ {tok(toks)}") if wide else s

    right = [pair(L("session"), st_c, st_t)]
    if wide:
        right.append(fg(C_MUTED, dur))
    out.append(pad(gauge("ctx", pct, ctx_txt), gw) + sep.join(right))

    for name, pv, gc, gt, pcost, ptoks, reset in (
        ("5h", h5p, g5c, g5t, p5c, p5t, h5_reset),
        ("7d", d7p, g7c, g7t, p7c, p7t, d7_reset),
    ):
        out.append(pad(gauge(name, pv, "↻ " + eta(reset)), gw) + sep.join([
            pair(L("total"), gc, gt),
            pair(L("project"), pcost, ptoks),
        ]))

    return "\n".join(out)


# ══════════════════════════════ COMMANDS ═══════════════════════════════════

SETTINGS_ENTRY = {"type": "command", "padding": 1, "refreshInterval": 30}


def cmd_install(targets=None):
    """Write the statusLine entry into every account's settings.json."""
    me = os.path.realpath(__file__)
    accounts = [Path(t).expanduser() for t in targets] if targets else find_accounts()
    if not accounts:
        print("no Claude Code account directory found under ~", file=sys.stderr)
        return 1

    for acc in accounts:
        settings = acc / "settings.json"
        try:
            current = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
            if not isinstance(current, dict):
                raise ValueError("settings.json is not a JSON object")
        except Exception as e:
            print(f"  {acc}: skipped, cannot read settings.json ({e})", file=sys.stderr)
            continue

        if settings.exists():
            backup = settings.with_suffix(f".json.bak-{int(time.time())}")
            shutil.copy2(settings, backup)
        else:
            backup = None

        # Quoted: Claude Code runs this through a shell and the path may
        # contain spaces.
        current["statusLine"] = dict(SETTINGS_ENTRY, command=f'python3 "{me}"')
        acc.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        note = f" (backup: {backup.name})" if backup else " (created)"
        print(f"  {settings}{note}")

    try:
        n, _ = fetch_prices()
        print(f"\nprices: {n} models fetched")
    except Exception as e:
        print(f"\nprices: fetch failed ({e}); falling back to built-in rates "
              f"until the next attempt", file=sys.stderr)

    print("\nRestart Claude Code to pick up the status line.")
    return 0


def cmd_doctor():
    ok = True
    print(f"python        {sys.version.split()[0]}  ({sys.executable})")
    print(f"script        {os.path.realpath(__file__)}")

    if PRICES_FILE.exists():
        blob = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
        age = time.time() - blob.get("ts", 0)
        state = "ok" if age < PRICES_TTL else ("stale" if age < PRICES_WARN_AFTER else "OUTDATED")
        print(f"prices        {len(blob.get('models', {}))} models, "
              f"{int(age // 86400)}d {int(age % 86400 // 3600)}h old — {state}")
    else:
        print("prices        missing — run --refresh-prices")
        ok = False

    me = os.path.realpath(__file__)
    accounts = find_accounts()
    if not accounts:
        print("accounts      none found")
        ok = False
    for acc in accounts:
        sessions = len(list((acc / "projects").glob("*/*.jsonl"))) if (acc / "projects").is_dir() else 0
        cmd, linked = "", False
        try:
            cmd = json.loads((acc / "settings.json").read_text()).get("statusLine", {}).get("command", "")
            linked = me in cmd
        except Exception:
            pass
        wired = ("wired to this file" if linked
                 else (f"wired elsewhere: {cmd}" if cmd else "not configured"))
        print(f"account       {acc}  ({sessions} sessions, {wired})")
        if not linked:
            ok = False

    set_config_dir(accounts[0] if accounts else Path.home() / ".claude")
    load_prices()
    probe = {"model": {"display_name": "Probe"}, "workspace": {"current_dir": os.getcwd()},
             "context_window": {"used_percentage": 25, "total_input_tokens": 50000,
                                "context_window_size": 200000}}
    t0 = time.time()
    render(probe)
    t1 = time.time()
    render(probe)
    print(f"render        {int((t1 - t0) * 1000)} ms cold, {int((time.time() - t1) * 1000)} ms warm")
    print("\n" + ("all good" if ok else "see the lines above"))
    return 0 if ok else 1


def cmd_prices():
    load_prices()
    if not PRICE_MAP:
        print("no price data — run --refresh-prices")
        return 1
    print(f"{len(PRICE_MAP)} models, {int(PRICES_AGE // 3600)}h old"
          f"   (USD/1M: in out cache-write-5m cache-read cache-write-1h)\n")
    for k in sorted(PRICE_MAP):
        i, o, w5, rd, w1 = PRICE_MAP[k]
        print(f"  {k:<34} {i:>7.2f} {o:>7.2f} {w5:>7.2f} {rd:>6.2f} {w1:>7.2f}")
    return 0


def cmd_refresh(quiet):
    try:
        n, changes = fetch_prices()
    except Exception as e:
        if not quiet:
            print(f"price fetch failed: {e}", file=sys.stderr)
        return 1
    if quiet:
        return 0
    print(f"fetched {n} models -> {PRICES_FILE}")
    if not changes:
        print("no changes since the last fetch")
    for mark, name, a, b in changes:
        if mark == "+":
            print(f"  + {name:<32} {b[0]:>7.2f} / {b[1]:>7.2f}")
        elif mark == "-":
            print(f"  - {name:<32} (dropped from the feed)")
        else:
            print(f"  ~ {name:<32} {a[0]:>7.2f} / {a[1]:>7.2f}"
                  f"  ->  {b[0]:>7.2f} / {b[1]:>7.2f}")
    return 0


HELP = f"""\
Claude Code status line {__version__} — {__author__}

  statusline.py                      render (reads the session JSON on stdin)
  statusline.py --install [DIR ...]  add the statusLine entry to settings.json
  statusline.py --doctor             check the setup and report timings
  statusline.py --prices             show the rate table in use
  statusline.py --refresh-prices     fetch rates now and show what changed
  statusline.py --clear-cache [DIR]  drop the cached scan and limit readings
  statusline.py --config-dir=DIR     force an account dir instead of detecting
  statusline.py --help               this summary

--install detects account directories under ~ (.claude, .claude-work, ...) and
backs up each settings.json before writing. Pass directories to override.

Prices refresh themselves once a day in the background, so no cron or launchd
job is needed. Edit the CONFIGURATION block at the top for layout and colours.
"""


def main():
    argv = sys.argv[1:]
    flags = {a for a in argv if a.startswith("-")}
    rest = [a for a in argv if not a.startswith("-")]
    cfg_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--config-dir=")), None)

    if "--help" in flags or "-h" in flags:
        print(HELP)
        return 0
    if "--refresh-prices" in flags:
        return cmd_refresh("--quiet" in flags)
    if "--install" in flags:
        return cmd_install(rest or None)

    set_config_dir(cfg_arg or (rest[0] if rest and "--clear-cache" in flags else None)
                   or os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))

    if "--doctor" in flags:
        return cmd_doctor()
    if "--prices" in flags:
        return cmd_prices()
    if "--clear-cache" in flags:
        for f in (CACHE_FILE, LIMITS_FILE, LOCK_FILE):
            try:
                f.unlink()
            except OSError:
                pass
        print(f"cache cleared for {CLAUDE_DIR}")
        return 0

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    set_config_dir(cfg_arg or detect_config_dir(data))
    load_prices()
    sys.stdout.write(render(data) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
