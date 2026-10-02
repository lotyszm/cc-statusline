"""Where time tracking keeps its data and configuration.

CC_STATUSLINE_DATA_DIR and CC_STATUSLINE_CONFIG override the XDG locations,
which is how the tests stay away from the real database. statusline.py finds
the status files the same way, so the two have to stay in step.
"""

import os
from pathlib import Path

NAME = "cc-statusline"


def data_dir():
    env = os.environ.get("CC_STATUSLINE_DATA_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / NAME


def config_path():
    env = os.environ.get("CC_STATUSLINE_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / NAME / "config.toml"


def db_path():
    return data_dir() / "tracker.db"


def status_dir():
    return data_dir() / "status"


def log_path():
    return data_dir() / "hook.log"
