"""Export of tracked time to FlowApp: proposed entries, their review state, the API.

The tracker's intervals become blocks - one FlowApp entry each: a task's time
on one local day, overlaps joined, short breaks glued, each block rounded up
to the configured step. Blocks a person edited, skipped or sent live in the
`flowapp_exports` table; the rest is recomputed on every request. Once a block
is sent, its stored values are what the page shows: no moment of tracked time
may reach FlowApp twice.

The token never leaves this module's callers' process: it is not written to
the config, the database, a log or any response of the dashboard.
"""

import os
import stat
import subprocess
import sys

from . import paths

TOKEN_ENV = "FLOWAPP_TOKEN"
KEYCHAIN_SERVICE = "flowapp-api-token"


def load_token(env=None, platform=None, run=None):
    """(token, None) or (None, problem): the FlowApp API token and where it failed.

    Looked up in FLOWAPP_TOKEN, then in the token file - only when nobody but
    the owner can read it, otherwise the problem is "token_file_mode" and the
    file stays unread - then, on macOS, in the Keychain. A login service does
    not see the shell's variables, hence the file and the Keychain.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    run = subprocess.run if run is None else run
    token = (env.get(TOKEN_ENV) or "").strip()
    if token:
        return token, None
    path = paths.flowapp_token_path()
    try:
        mode = path.stat().st_mode
    except OSError:
        mode = None
    if mode is not None:
        if stat.S_IMODE(mode) & 0o077:
            return None, "token_file_mode"
        try:
            token = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            token = ""
        if token:
            return token, None
    if platform == "darwin":
        try:
            r = run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            r = None
        token = (r.stdout or "").strip() if r is not None and r.returncode == 0 else ""
        if token:
            return token, None
    return None, "no_token"
