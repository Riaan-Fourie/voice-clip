"""Shared utilities for VoiceClip modules."""

import os
from datetime import datetime

STATE_DIR = os.path.expanduser("~/.voice-clip")
LOG_PATH = os.path.join(STATE_DIR, "voiceclip-debug.log")


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
