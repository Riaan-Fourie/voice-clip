# VoiceClip

macOS speech-to-text tool. Hold Right Command to record, release to transcribe, result auto-pastes into the focused window.

Runs entirely locally on Apple Silicon using mlx-whisper (Metal GPU). No cloud APIs, no internet required.

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
- **Accessibility permission** for the terminal/app running it (for CGEventTap)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
./venv/bin/python main.py
```

Do **not** use LaunchAgent — CGEventTap requires Accessibility permission which doesn't carry over through launchd.

## Key Design Decisions

- **mlx-whisper** over Apple SFSpeechRecognizer — SFSpeechRecognizer crashes from Python due to broken TCC code signature chain
- **CGEventTap** over pynput/NSEvent — pynput needs Input Monitoring; NSEvent can't detect modifier-only keys; CGEventTap works with Accessibility only
- **Right Command** over Fn — macOS intercepts Fn for dictation/emoji
- **NSPanel** over NSWindow — NSPanel floats above fullscreen spaces
