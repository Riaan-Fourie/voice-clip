#!/bin/bash
# VoiceClip launcher for launchd
# Runs main.py directly — launchd handles restarts via KeepAlive.

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Ensure log directory exists
mkdir -p ~/.voice-clip

# Wait for WindowServer to be available (needed at login)
for i in $(seq 1 30); do
    if pgrep -x WindowServer >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Small extra delay to let the GUI session fully initialize
sleep 2

exec "$DIR/venv/bin/python" "$DIR/main.py"
