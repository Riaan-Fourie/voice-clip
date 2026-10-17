#!/bin/bash
# Install VoiceClip's headless launch: the launcher app and its LaunchAgent (#407).
#
# Builds ~/Applications/VoiceClip Launcher.app from launcher/main.swift, then makes
# com.riaanfourie.voiceclip start it at login, with KeepAlive. No Terminal window.
# Why a launcher app at all: see the header of launcher/main.swift.
#
# The app is built ONCE. Its Accessibility grant is pinned to the binary's hash, so
# a rebuild means switching Accessibility on again by hand. Re-running this script
# keeps the installed app and only rewrites the LaunchAgent. Pass --rebuild after
# changing main.swift, then switch the new build on in System Settings.
#
# Usage: scripts/install-launcher.sh [--rebuild]

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# VoiceClip runs from the MAIN checkout, where the venv lives, even when this
# script is run from a worktree.
MAIN="$(cd "$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)/.." && pwd)"
STATE="$HOME/.voice-clip"
APP="$HOME/Applications/VoiceClip Launcher.app"
EXE="$APP/Contents/MacOS/VoiceClipLauncher"
LABEL="com.riaanfourie.voiceclip"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

[ -x "$MAIN/venv/bin/python" ] || { echo "ERROR: no venv at $MAIN/venv" >&2; exit 1; }
mkdir -p "$STATE" "$HOME/Applications" "$HOME/Library/LaunchAgents"

if [ ! -x "$EXE" ] || [ "${1:-}" = "--rebuild" ]; then
  STAGE="$(mktemp -d)/VoiceClip Launcher.app"
  mkdir -p "$STAGE/Contents/MacOS"
  cat > "$STAGE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.riaanfourie.voiceclip-launcher</string>
    <key>CFBundleName</key>
    <string>VoiceClip Launcher</string>
    <key>CFBundleExecutable</key>
    <string>VoiceClipLauncher</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>VoiceClip records your voice to transcribe it.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>VoiceClip can pause music while you dictate.</string>
    <key>VCRepoDir</key>
    <string>$MAIN</string>
</dict>
</plist>
EOF
  swiftc -O -o "$STAGE/Contents/MacOS/VoiceClipLauncher" "$REPO/launcher/main.swift"
  codesign --force --sign - "$STAGE"
  [ -d "$APP" ] && mv "$APP" "$HOME/.Trash/VoiceClip Launcher $(date +%s).app"
  mv "$STAGE" "$APP"
  echo "==> built $APP"
  echo "    switch it on: System Settings > Privacy & Security > Accessibility"
  echo "    then restart it: launchctl kickstart -k $DOMAIN/$LABEL"
else
  echo "==> kept $APP (pass --rebuild to replace it)"
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$EXE</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <!-- A crash or kill restarts it. A clean quit from the menu stays quit. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$STATE/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE/stderr.log</string>
</dict>
</plist>
EOF
echo "==> wrote $PLIST"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "==> loaded $LABEL"
