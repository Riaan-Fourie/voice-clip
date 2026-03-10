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
    # Read from git tag, or fall back to 1.0.0
    VERSION=$(git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || echo "1.0.0")
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
pip install -q py2app

# ── py2app build ──────────────────────────────────────────────────────────────
echo "▶ Running py2app..."
rm -rf dist build_py2app

# Stamp version into setup_app.py via env var (read by setup_app.py if present)
export VOICECLIP_VERSION="$VERSION"

python setup_app.py py2app --dist-dir dist --build-dir build_py2app 2>&1

APP_PATH="dist/VoiceClip.app"

if [[ ! -d "$APP_PATH" ]]; then
    echo "✗ py2app failed — dist/VoiceClip.app not found"
    exit 1
fi

echo "▶ py2app succeeded: $APP_PATH"

# ── Ad-hoc code sign ─────────────────────────────────────────────────────────
echo "▶ Ad-hoc signing (codesign)..."
codesign --deep --force --sign - "$APP_PATH"
echo "▶ Signed"

# ── Bundle Whisper model ──────────────────────────────────────────────────────
# The model is downloaded by mlx-whisper on first run to ~/.cache/huggingface/
# We do NOT bundle it in the .app to keep the .dmg small (~50MB vs ~200MB).
# Users see a one-time download notice on first use.
echo "▶ Skipping model bundle (downloads on first run)"

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
