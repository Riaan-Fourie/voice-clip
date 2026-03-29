# VoiceClip

macOS speech-to-text tool. Hold Right Command to record, release to transcribe, result auto-pastes into the focused window.

## Demo

<video src="https://github.com/Riaan-Fourie/voice-clip/raw/main/demo/voiceclip-demo.mp4" controls width="100%"></video>

## Download

**[→ Download latest VoiceClip.dmg](../../releases/latest)**

> Requires Apple Silicon (M1/M2/M3/M4) and macOS 13+.
>
> **First launch:** macOS will warn "unidentified developer" — right-click → Open to bypass it. You only need to do this once.

Runs locally on Apple Silicon using mlx-whisper (Metal GPU). No cloud APIs. Internet is only needed once for the first model download.

## How It Works

1. Hold **Right Command** key — recording starts with a floating VU meter overlay
2. Release — audio is transcribed locally via mlx-whisper
3. Result is copied to clipboard and auto-pasted (simulated Cmd+V) into whatever app is focused

## Architecture

| File | Purpose |
|------|---------|
| `main.py` | rumps menubar app + glue logic + auto-paste |
| `hotkey.py` | CGEventTap on main run loop for Right Command detection |
| `recorder.py` | sounddevice audio capture (built-in mic), RMS for VU meter |
| `transcriber.py` | mlx-whisper (`mlx-community/whisper-base-mlx`) on Metal GPU |
| `overlay.py` | Floating VU meter (NSPanel + custom NSView, works in fullscreen) |

## Requirements

- macOS (Apple Silicon)
- Python 3.10+
- **Accessibility permission** for VoiceClip/terminal (for CGEventTap)
- **Microphone permission** (macOS prompt on first recording)

## Install

```bash
./install.sh
```

Installer output:
- Creates `~/.voiceclip` (app + venv)
- Creates `/Applications/VoiceClip.app`
- Removes any legacy LaunchAgent from older installs

## Usage

Installed app:
- Launch `/Applications/VoiceClip.app`

Development run from this repo:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./venv/bin/python main.py
```

Do **not** run VoiceClip via LaunchAgent/`launchd` for hotkey capture.

Optional auto-start: add `/Applications/VoiceClip.app` to Login Items.

## Uninstall

```bash
./install.sh --uninstall
```

## Key Design Decisions

- **mlx-whisper** over Apple SFSpeechRecognizer — SFSpeechRecognizer crashes from Python due to broken TCC code signature chain
- **CGEventTap** over pynput/NSEvent — pynput needs Input Monitoring; NSEvent can't detect modifier-only keys; CGEventTap works with Accessibility only
- **Right Command** over Fn — macOS intercepts Fn for dictation/emoji
- **NSPanel** over NSWindow — NSPanel floats above fullscreen spaces

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full module breakdown, threading model, and design rationale.
See [GOTCHAS.md](GOTCHAS.md) for macOS-specific pitfalls and workarounds.
See [CHANGELOG.md](CHANGELOG.md) for version history.
