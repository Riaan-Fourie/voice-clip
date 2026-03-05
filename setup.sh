#!/bin/bash
# VoiceClip — setup script
# Creates venv, installs deps, installs LaunchAgent for auto-start

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PLIST_NAME="com.voiceclip.app"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "=== VoiceClip Setup ==="
echo ""

# 1. Create virtual environment
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
else
    echo "Virtual environment already exists."
fi

# 2. Install dependencies
echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
echo "Dependencies installed."

# 3. Install LaunchAgent for auto-start on login
echo ""
echo "Installing LaunchAgent for auto-start..."

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python</string>
        <string>$SCRIPT_DIR/main.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>StandardOutPath</key>
    <string>$HOME/.voice-clip/voiceclip.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.voice-clip/voiceclip.error.log</string>
</dict>
</plist>
EOF

mkdir -p "$HOME/.voice-clip"

# Load the agent
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "LaunchAgent installed and loaded."

echo ""
echo "=== Setup Complete ==="
echo ""
echo "VoiceClip is now running and will auto-start on login."
echo ""
echo "PERMISSIONS NEEDED (macOS will prompt on first use):"
echo "  1. Microphone access — allow when prompted"
echo "  2. Speech Recognition — allow when prompted"
echo "  3. Accessibility — grant in System Settings > Privacy & Security > Accessibility"
echo "     (Add Terminal or your IDE to the list)"
echo ""
echo "USAGE:"
echo "  Hold Right Option (⌥) key → speak → release → text is in your clipboard"
echo ""
echo "To stop:  launchctl unload $PLIST_PATH"
echo "To start: launchctl load $PLIST_PATH"
echo "Logs:     ~/.voice-clip/voiceclip.log"
