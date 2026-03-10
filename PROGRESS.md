# VoiceClip — Progress

## 2026-03-04 — Initial Build
- Created project structure in `repos/voice-clip/`
- Implemented `recorder.py` — sounddevice audio capture with RMS level callback for VU meter
- Implemented `transcriber.py` — initially Apple SFSpeechRecognizer, then rewrote to mlx-whisper
- Implemented `overlay.py` — floating borderless VU meter with animated bars (PyObjC)
- Implemented `main.py` — rumps menubar app, clipboard integration
- Implemented `hotkey.py` — CGEventTap on main run loop for Right Command key
- Created `setup.sh` — venv, deps, LaunchAgent auto-start on login
- Created CLAUDE.md, .gitignore

## 2026-03-04 — Fixes & Improvements
- Switched from Apple SFSpeechRecognizer to mlx-whisper (TCC crash fix)
- Switched from pynput to CGEventTap (Input Monitoring → Accessibility only)
- Changed hotkey from Right Option → Right Command (more reliable detection)
- Set MacBook Air mic as preferred input device (over AirPods Bluetooth)
- Increased VU meter sensitivity (divisor 8000 → 1500)
- Changed overlay from NSWindow to NSPanel for fullscreen rendering
- Added auto-paste via simulated Cmd+V after transcription

## 2026-03-10 — Public Release Readiness
- Replaced risky stale-process killing in `main.py` with safe single-instance file locking.
- Ensured `~/.voice-clip` exists before writing runtime logs.
- `hotkey.py`: modifier-only `flagsChanged` handling for Right Command.
- `overlay.py`: correct NSTimer selector signature + main-thread show/hide robustness.
- `install.sh` is canonical and handles `--uninstall` first.
- Removed LaunchAgent-based startup from install flow.
- Cleans up legacy LaunchAgent from older installs.
- Simplified `setup.sh` to a deprecated wrapper that forwards to `install.sh`.
- Updated docs (`README.md`, `CLAUDE.md`) to match actual runtime behavior and permissions.
- Added `LICENSE` (MIT).
