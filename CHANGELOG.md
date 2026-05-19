# Changelog

All notable changes to VoiceClip are documented here.

## [Unreleased]

### Added
- `~/.voice-clip/proper_nouns.txt` — user-editable list piped into Whisper's `initial_prompt`. Fixes recurring misreads of names and jargon (e.g. "Dewald", "HashDirectors", "SumSub"). Auto-created with sensible defaults on first run. Edit and call `Transcriber.reload_proper_nouns()` to refresh without restart.

### Changed
- Swapped default model from `whisper-small.en-mlx` to `whisper-large-v3-turbo`. Large-model accuracy at ~5-8x large-v3 speed; multilingual base also handles non-English names better than the `.en` variant.
- Decode WAV audio in memory and pass numpy arrays directly to `mlx_whisper.transcribe()`. Removes three disk round-trips per transcription.
- Extracted shared `_log()` function into `utils.py` (was duplicated across all modules).
- Moved `test_keys.py` to `scripts/test_keys.py`.
- Converted `CLAUDE.md` to `ARCHITECTURE.md`.
- Added basic pytest test suite.

### Removed
- `noisereduce` from the transcription hot path (and `requirements.txt`). Spectral-gate denoising on CPU added 1-3s per clip on a built-in MacBook mic where it was buying close to nothing.
- `soundfile` dependency (no longer needed once disk round-trip was removed).

### Fixed
- Call `mlx.core.clear_cache()` after every transcription to release Metal GPU workspace buffers. Without this, IOAccelerator pages accumulate across calls — a single idle VoiceClip was observed at ~1.6 GB footprint (1.4 GB of it GPU tensors). Weights stay warm; only transient compute memory is freed.

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
