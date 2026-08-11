#!/bin/bash
# Install (or refresh) the VoiceClip watchdog LaunchAgent.
#
# The watchdog used to live ONLY at ~/.voice-clip/watchdog.sh, untracked — so the
# thing responsible for keeping VoiceClip alive was the one piece of VoiceClip with
# no version history and no review. This script makes the repo the source of truth.
#
# Idempotent: safe to re-run after every pull.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$HOME/.voice-clip"
AGENT_LABEL="com.riaanfourie.voiceclip-watchdog"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"

mkdir -p "$STATE" "$HOME/Library/LaunchAgents"

install -m 755 "$REPO/scripts/watchdog.sh" "$STATE/watchdog.sh"
echo "==> installed $STATE/watchdog.sh"

cat > "$AGENT_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$AGENT_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$STATE/watchdog.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>120</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>StandardOutPath</key>
    <string>$STATE/watchdog-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE/watchdog-launchd.log</string>
</dict>
</plist>
EOF
echo "==> wrote $AGENT_PLIST"

launchctl unload "$AGENT_PLIST" 2>/dev/null || true
launchctl load "$AGENT_PLIST"
echo "==> loaded $AGENT_LABEL (polls every 120s)"
