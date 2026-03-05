#!/bin/bash
# VoiceClip Installer
# Installs VoiceClip as a macOS menubar app with auto-start on login.
#
# Requirements: macOS 13+, Apple Silicon, Python 3.10+
# Usage: ./install.sh

set -e

INSTALL_DIR="$HOME/.voiceclip"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
BUNDLE_DIR="/Applications/VoiceClip.app"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.riaanfourie.voiceclip.plist"
LOG_DIR="$HOME/.voice-clip"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=============================="
echo "  VoiceClip Installer"
echo "=============================="
echo ""

# Check Apple Silicon
if [[ "$(uname -m)" != "arm64" ]]; then
    echo "ERROR: VoiceClip requires Apple Silicon (M1/M2/M3/M4)."
    echo "mlx-whisper only runs on Apple Silicon with Metal GPU."
    exit 1
fi

# Check Python
PYTHON=""
for p in python3.11 python3.12 python3.13 python3; do
    if command -v "$p" &>/dev/null; then
        version=$("$p" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
            PYTHON="$p"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python 3.10+ required. Install with: brew install python@3.11"
    exit 1
fi
echo "Using Python: $PYTHON ($($PYTHON --version))"

# Create install directory
echo ""
echo "Installing to $INSTALL_DIR..."
mkdir -p "$APP_DIR" "$LOG_DIR"

# Copy source files
echo "Copying app files..."
for f in main.py recorder.py transcriber.py overlay.py hotkey.py; do
    cp "$SOURCE_DIR/$f" "$APP_DIR/"
done

# Create/update venv
if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

echo "Installing dependencies (this may take a minute)..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet \
    "rumps>=0.4.0" \
    "sounddevice>=0.4.6" \
    "numpy>=1.24.0" \
    "mlx-whisper>=0.4.0" \
    "pyobjc-framework-Cocoa>=10.0" \
    "pyobjc-framework-Quartz>=10.0"

# Create launcher script
cat > "$INSTALL_DIR/run.sh" << 'LAUNCHER'
#!/bin/bash
cd "$(dirname "$0")/app"
exec "$(dirname "$0")/venv/bin/python" main.py
LAUNCHER
chmod +x "$INSTALL_DIR/run.sh"

# Create .app bundle in /Applications
echo "Creating VoiceClip.app..."
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR/Contents/MacOS"
mkdir -p "$BUNDLE_DIR/Contents/Resources"

# App launcher
cat > "$BUNDLE_DIR/Contents/MacOS/VoiceClip" << APPLAUNCHER
#!/bin/bash
exec "$INSTALL_DIR/run.sh"
APPLAUNCHER
chmod +x "$BUNDLE_DIR/Contents/MacOS/VoiceClip"

# Info.plist
cat > "$BUNDLE_DIR/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>VoiceClip</string>
    <key>CFBundleIdentifier</key>
    <string>com.riaanfourie.voiceclip</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleExecutable</key>
    <string>VoiceClip</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>VoiceClip needs microphone access to record your voice for speech-to-text.</string>
</dict>
</plist>
PLIST

# LaunchAgent for auto-start
echo "Setting up auto-start on login..."
cat > "$LAUNCH_AGENT" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.riaanfourie.voiceclip</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_DIR/run.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/voiceclip.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/voiceclip.error.log</string>
</dict>
</plist>
PLIST

# Load the LaunchAgent
launchctl unload "$LAUNCH_AGENT" 2>/dev/null || true
launchctl load -w "$LAUNCH_AGENT"

echo ""
echo "=============================="
echo "  VoiceClip installed!"
echo "=============================="
echo ""
echo "  App:        /Applications/VoiceClip.app"
echo "  Install:    $INSTALL_DIR"
echo "  Logs:       $LOG_DIR/voiceclip-debug.log"
echo ""
echo "  Hotkey:     Hold Right Command (⌘) to record"
echo "              Release to transcribe & auto-paste"
echo ""
echo "  First run will download the Whisper model (~150MB)."
echo ""
echo "  PERMISSIONS NEEDED (one-time):"
echo "  1. Accessibility — System Settings > Privacy > Accessibility"
echo "     Add 'VoiceClip' or grant to Terminal/iTerm"
echo "  2. Microphone — macOS will prompt on first recording"
echo ""
echo "  To uninstall: ./install.sh --uninstall"
echo ""

# Handle --uninstall flag
if [[ "$1" == "--uninstall" ]]; then
    echo "Uninstalling VoiceClip..."
    launchctl unload "$LAUNCH_AGENT" 2>/dev/null || true
    rm -f "$LAUNCH_AGENT"
    rm -rf "$BUNDLE_DIR"
    rm -rf "$INSTALL_DIR"
    echo "VoiceClip uninstalled. Logs at $LOG_DIR were kept."
    exit 0
fi
