#!/bin/bash
# build_macos.sh — Build VoiceClip.dmg for macOS distribution
#
# Usage:
#   ./build/build_macos.sh              # builds dist/VoiceClip-<version>.dmg
#   ./build/build_macos.sh --version 1.2.0
#
# Requirements:
#   - Apple Silicon Mac (mlx-whisper is ARM-only)
#   - Python 3.10+ in a venv at ./venv (created automatically if missing)
#
# Output: dist/VoiceClip-<version>.dmg

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── Version ───────────────────────────────────────────────────────────────────
VERSION=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --version) VERSION="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -z "$VERSION" ]]; then
    VERSION=$(git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//')
    VERSION="${VERSION:-1.0.0}"
fi

echo "▶ Building VoiceClip v$VERSION"

# ── Venv + deps ───────────────────────────────────────────────────────────────
if [[ ! -d venv ]]; then
    echo "▶ Creating venv..."
    python3 -m venv venv
fi

source venv/bin/activate

echo "▶ Installing build deps..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q pyinstaller

# ── PyInstaller build ─────────────────────────────────────────────────────────
echo "▶ Running PyInstaller..."
rm -rf dist build_pyinstaller

pyinstaller \
    --name VoiceClip \
    --windowed \
    --noconfirm \
    --distpath dist \
    --workpath build_pyinstaller \
    --osx-bundle-identifier com.riaanfourie.voiceclip \
    --collect-all mlx \
    --collect-all mlx_whisper \
    --collect-all rumps \
    --collect-all sounddevice \
    --collect-all huggingface_hub \
    --collect-all tokenizers \
    --collect-all safetensors \
    --hidden-import objc \
    --hidden-import AppKit \
    --hidden-import Foundation \
    --hidden-import Quartz \
    --hidden-import PyObjCTools \
    --collect-all numba \
    --collect-all llvmlite \
    --exclude-module torch \
    --exclude-module tensorflow \
    --exclude-module keras \
    --exclude-module sklearn \
    --exclude-module pandas \
    --exclude-module matplotlib \
    main.py 2>&1

APP_PATH="dist/VoiceClip.app"

if [[ ! -d "$APP_PATH" ]]; then
    echo "✗ PyInstaller failed — dist/VoiceClip.app not found"
    exit 1
fi

echo "▶ PyInstaller succeeded: $APP_PATH"

# ── Patch Info.plist ──────────────────────────────────────────────────────────
# PyInstaller creates a basic Info.plist; we patch in required macOS keys
echo "▶ Patching Info.plist..."
PLIST="$APP_PATH/Contents/Info.plist"

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST"

/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"

# Hide from Dock (menubar-only app)
/usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST"

# Microphone usage description (required for permission prompt)
/usr/libexec/PlistBuddy -c "Set :NSMicrophoneUsageDescription 'VoiceClip needs microphone access to record your voice for speech-to-text.'" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string 'VoiceClip needs microphone access to record your voice for speech-to-text.'" "$PLIST"

echo "▶ Info.plist patched"

# ── Strip quarantine ─────────────────────────────────────────────────────────
# Removes the com.apple.quarantine xattr so the locally built app launches
# without Gatekeeper blocking it. (Downloaded DMGs will still get quarantine
# applied by the browser — that's expected for unsigned apps.)
echo "▶ Stripping quarantine xattr..."
xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true
echo "▶ Quarantine stripped"

# ── Ad-hoc code sign ─────────────────────────────────────────────────────────
echo "▶ Ad-hoc signing (codesign)..."
codesign --deep --force --sign - "$APP_PATH"
echo "▶ Signed"

# ── Create .dmg ───────────────────────────────────────────────────────────────
echo "▶ Creating .dmg..."

DMG_NAME="VoiceClip-v${VERSION}.dmg"
DMG_PATH="dist/$DMG_NAME"
STAGING_DIR="dist/dmg-staging"

rm -rf "$STAGING_DIR" "$DMG_PATH"
mkdir -p "$STAGING_DIR"

cp -r "$APP_PATH" "$STAGING_DIR/VoiceClip.app"

# Symlink to /Applications for drag-to-install UX
ln -s /Applications "$STAGING_DIR/Applications"

hdiutil create \
    -volname "VoiceClip" \
    -srcfolder "$STAGING_DIR" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG_PATH"

rm -rf "$STAGING_DIR"

echo ""
echo "✓ Done: dist/$DMG_NAME"
echo ""
echo "Install tip for users:"
echo "  macOS will block the first launch. Right-click → Open to bypass Gatekeeper."
