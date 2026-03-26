# Changelog

All notable changes to VoiceClip are documented here.

## [Unreleased]

### Changed
- Extracted shared `_log()` function into `utils.py` (was duplicated across all modules).
- Moved `test_keys.py` to `scripts/test_keys.py`.
- Converted `CLAUDE.md` to `ARCHITECTURE.md`.
- Added basic pytest test suite.

## [0.2.0] — 2026-03-10

### Changed
- Replaced stale-process killing with safe single-instance advisory file lock (`fcntl.flock`).
- `install.sh` is now the canonical installer; handles `--uninstall` cleanly.
- Removed LaunchAgent-based startup (broken for CGEventTap — see GOTCHAS.md).
- Simplified `setup.sh` to a deprecated wrapper forwarding to `install.sh`.

### Fixed
- Ensured `~/.voice-clip` directory exists before writing runtime logs.
- Correct NSTimer selector signature in overlay (`v@:@`).
- Main-thread dispatch robustness for overlay show/hide.

### Added
- MIT License.

## [0.1.0] — 2026-03-04

### Added
- Initial implementation: hold Right Command to record, release to transcribe, auto-paste result.
- `main.py` — rumps menubar app with status icons and notifications.
- `hotkey.py` — CGEventTap on main run loop for Right Command key detection.
- `recorder.py` — sounddevice audio capture with RMS level callback for VU meter.
- `transcriber.py` — mlx-whisper on Apple Silicon Metal GPU.
- `overlay.py` — floating VU meter (NSPanel + custom NSView, works in fullscreen).
- Failed recording retry via menubar action.
- Clipboard copy + simulated Cmd+V auto-paste.

### Changed
- Switched from Apple SFSpeechRecognizer to mlx-whisper (TCC crash fix).
- Switched from pynput to CGEventTap (Input Monitoring to Accessibility only).
- Changed hotkey from Right Option to Right Command (more reliable detection).
- Set MacBook Air built-in mic as preferred input device.
- Increased VU meter sensitivity (divisor 8000 to 1500).
- Changed overlay from NSWindow to NSPanel for fullscreen rendering.
