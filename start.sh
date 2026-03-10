#!/bin/bash
# VoiceClip local launcher

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Ensure log directory exists
mkdir -p ~/.voice-clip

exec "$DIR/venv/bin/python" "$DIR/main.py"
