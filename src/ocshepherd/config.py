"""Config file handling for opencode-shepherd.

Optional, user-editable settings live in ``~/.config/ocshepherd/config.json``
(respecting ``$XDG_CONFIG_HOME``). Everything is optional — missing file or
missing keys fall back to defaults. ``watch`` reads the file fresh on each
evaluation, so ``ocshepherd config set ...`` takes effect live.

Keys:
    enabled               bool    master switch for notifications (default true)
    remind_after_minutes  int     re-notify every N minutes while an item is
                                  still waiting (default null = off)
    snippet               int     max chars of last-message snippets (default 220)
    state                 str     state file path override (default XDG state)
    telegram_token        str     Telegram bot token (default null)
    telegram_chat         str     Telegram chat id (default null)
    notify_cmd            str     shell command receiving alerts on stdin
    resync_minutes        int     event mode: full check safety net (default 30)
    min_interval_seconds  int     event mode: min seconds between checks (default 15)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "enabled": True,
    "remind_after_minutes": None,
    "snippet": 220,
    "state": None,
    "telegram_token": None,
    "telegram_chat": None,
    "notify_cmd": None,
    "resync_minutes": 30,
    "min_interval_seconds": 15,
}


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ocshepherd" / "config.json"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(config_path()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS or k})
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def coerce(value: str):
    """Parse a CLI string into a JSON-friendly value (true/false/null/int/float/str)."""
    v = value.strip()
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none", ""):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v
