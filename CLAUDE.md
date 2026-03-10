# VoiceClip

## Overview
macOS speech-to-text app. Hold Right Command key to record, release to transcribe, result auto-pastes into focused window. Replaces Whispr Flow.

## Recent Update (2026-03-10)
- Runtime hardening: single-instance lock + guaranteed log directory creation.
- Installer hardening: `install.sh` is canonical, `--uninstall` handled first, legacy LaunchAgent removed.
- Behavior/docs consistency: startup guidance now explicitly avoids LaunchAgent/launchd for hotkey capture.
- Added MIT `LICENSE`.

## Architecture
- **main.py** — rumps menubar app + CGEventTap hotkey + glue logic + auto-paste via simulated Cmd+V
- **recorder.py** — sounddevice audio capture (MacBook Air mic preferred), RMS level callback for VU meter
- **transcriber.py** — mlx-whisper (local Whisper on Apple Silicon via Metal GPU), model: `mlx-community/whisper-base-mlx`
- **overlay.py** — floating VU meter overlay (NSPanel + custom NSView, renders above fullscreen apps)
- **hotkey.py** — CGEventTap on main run loop for Right Command key detection (Accessibility permission only)
- **start.sh** — local launcher for `main.py`
- **install.sh** — canonical installer/uninstaller (`./install.sh`, `./install.sh --uninstall`)
- **setup.sh** — deprecated wrapper that forwards to `install.sh`

## Key Behavior
- Hold Right Command → record with VU meter overlay → release → transcribe → auto-paste into focused window
- Also copies to clipboard as backup
- Failed transcriptions saved to `~/.voice-clip/failed/` with retry via menubar
- Optional auto-start via macOS Login Items (`/Applications/VoiceClip.app`)
- Logs at `~/.voice-clip/voiceclip-debug.log`
- Always uses MacBook Air built-in mic (better quality than Bluetooth)

## Dependencies
rumps, sounddevice, numpy, mlx-whisper, pyobjc-framework-Cocoa, pyobjc-framework-Quartz

## Permissions
- **Accessibility** — for CGEventTap hotkey detection
- **Microphone** — macOS prompts on first use

## Key Decisions
- **mlx-whisper over Apple SFSpeechRecognizer** — Apple Speech requires TCC SpeechRecognition permission which crashes when run from Python (broken code signature chain). mlx-whisper runs 100% locally on Metal GPU with no TCC requirements.
- **CGEventTap over pynput/NSEvent** — pynput needs Input Monitoring permission (user rejected). NSEvent.addGlobalMonitorForEvents doesn't fire for modifier-only keys. CGEventTap on main run loop works with Accessibility permission only.
- **Right Command over Fn** — Fn is intercepted by macOS for dictation/emoji picker.
- **NSPanel over NSWindow for overlay** — NSPanel with NonactivatingPanel can float above fullscreen spaces; NSWindow cannot.

## Progress
See PROGRESS.md
