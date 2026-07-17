"""Shared utilities for VoiceClip modules."""

import json
import os
from datetime import datetime

STATE_DIR = os.path.expanduser("~/.voice-clip")
LOG_PATH = os.path.join(STATE_DIR, "voiceclip-debug.log")
SETTINGS_PATH = os.path.join(STATE_DIR, "settings.json")


def load_settings() -> dict:
    """Read persisted settings; a missing or corrupt file is just defaults."""
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict):
    """Persist settings atomically (write temp + rename)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def _log(msg, tag=None):
    """Write a timestamped message directly to the log file (no buffering issues).

    Args:
        msg: The message to log.
        tag: Optional tag prefix, e.g. "hotkey", "overlay", "transcriber".
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    prefix = f" [{tag}]" if tag else ""
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.now().isoformat()}{prefix} {msg}\n")
