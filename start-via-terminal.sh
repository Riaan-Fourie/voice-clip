#!/bin/bash
# Launch VoiceClip via Terminal.app so CGEventTap inherits Accessibility permission.
# Designed to be called from a LaunchAgent at login.

DIR="$(cd "$(dirname "$0")" && pwd)"

osascript -e "
tell application \"Terminal\"
    do script \"cd '$DIR' && ./venv/bin/python main.py; exit\"
end tell
"
