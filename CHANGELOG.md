# Changelog

All notable changes to VoiceClip are documented here.

## [Unreleased]

### Fixed
- **Transition masking is now OFF by default (Jarvis #266).** The volume fade fought the user's own volume changes mid-dictation (restore wrote back a stale saved level — music swung loud/soft). Fade and Auto-Pause remain in the Transition submenu as explicit opt-ins only.
- **Stale PortAudio device table killed dictation silently after AirPods reconnect (Jarvis #265).** Bluetooth devices get a new CoreAudio ID on every reconnect, but PortAudio snapshots devices at init — so in a long-running process every `InputStream` open (including the system-default retry) failed with `-9986`, and the press path surfaced nothing: volume ducked, then… silence. Now: on open failure the recorder re-initializes PortAudio, re-resolves the preference, retries, then falls back to system default; and `main.py` wraps `recorder.start()` so any mic failure shows a ⚠️ status + macOS notification instead of a silent no-op (and can never raise into the CGEventTap callback).

### Added
- **HFP-flip transition masking (Jarvis #264).** Recording from AirPods flips the Bluetooth link to the hands-free profile with a harsh audible clip (OS-level, can't be removed — the reason #190 was reverted). New `Transition` submenu masks it, persisted to settings.json: **Volume Fade** (default — duck output volume before the mic opens so the flip lands in near-silence, ramp back up while recording, duck again around close, restore ~1.2s after) or **Auto-Pause Music** (pause Spotify/Music if playing, resume after the link flips back) or **Off**. Only engages for Bluetooth-named input devices — the MacBook mic gets zero masking overhead. Rapid re-press carries the saved volume / paused players forward so nothing gets stuck ducked or paused. All AppleScript is best-effort and can never delay-fail or abort a recording. Probed studio-quality recording (macOS 26 + AirPods Pro 3): VoiceClip's CoreAudio path gets the 24 kHz wideband voice path, not the 48 kHz studio path (gated to Apple capture APIs) — masking, not codec, is the fix.
- **Microphone picker with AirPods default (Jarvis #263).** New `Microphone` submenu in the menu bar (AirPods / MacBook Mic / System Default), check-marked and persisted to `~/.voice-clip/settings.json`. Default preference is now AirPods, falling back to the MacBook mic, then the system default, when not connected — resolution happens at every recording start, so pairing/unpairing AirPods between recordings just works. A stale device index at stream-open time retries on the system default rather than losing the recording. The recording status line now names the mic actually in use. Note: AirPods record over Bluetooth HFP (narrowband), so transcription accuracy is somewhat lower than the built-in mic — the toggle makes switching back one click.
- **Self-healing for a transcription wedged in native code (issue #187 / jarvis-system #6).** A hang *inside* `mlx_whisper.transcribe` (native Metal) never returns, leaving the worker thread alive — the #170 flag-reset couldn't recover it, so dictation stayed dead until a manual `kill`+restart (incident 2026-06-30). A new background `_transcribe_watchdog` now re-execs the process (`os.execv`) when a transcription is stuck past `TRANSCRIBE_STUCK_TIMEOUT` **and** its worker thread is still alive (a true native wedge); a merely leaked gate still gets the cheap flag reset. `execv` keeps the PID but discards all in-process state — the automatic equivalent of the manual restart. Self-relaunch via `execv` (not self-exit) because there is deliberately no LaunchAgent (it breaks the CGEventTap). The instance-lock fd is now `O_CLOEXEC` so the exec releases and re-acquires it cleanly. Witnessed by `tests/manual_reexec.py`; unit-locked in `tests/test_transcribe_wedge.py`.
- `~/.voice-clip/proper_nouns.txt` — user-editable list piped into Whisper's `initial_prompt`. Fixes recurring misreads of names and jargon (e.g. "Dewald", "HashDirectors", "SumSub"). Auto-created with sensible defaults on first run. Edit and call `Transcriber.reload_proper_nouns()` to refresh without restart.

### Changed
- **Reverted default model to `whisper-small.en-mlx` (2026-06-15).** `distil-whisper-large-v3` (1.4 GB) was fast only while warm — on this 16 GB Mac its weights paged to swap when idle, so the next dictation stalled faulting them back in (diagnosed via `pmset`: zero thermal throttle, swap at 14.9/15 GB — memory pressure, not heat). small.en (459 MB) is light enough to survive memory pressure and stay instant; the proper-noun `initial_prompt` mitigates the accuracy gap. (Superseded the 2026-06-11 swap to distil-large-v3, which is kept below for history.)
- Swapped default model from `whisper-small.en-mlx` to `distil-whisper-large-v3` (2026-06-11). Near large-v3 accuracy, English-only. Benchmarked on M5: 23x realtime vs small.en's 46x — latency difference imperceptible for short dictations. (Earlier `large-v3-turbo` attempt was reverted on the old machine for being too slow.)
- Decode WAV audio in memory and pass numpy arrays directly to `mlx_whisper.transcribe()`. Removes three disk round-trips per transcription.
- Extracted shared `_log()` function into `utils.py` (was duplicated across all modules).
- Moved `test_keys.py` to `scripts/test_keys.py`.
- Converted `CLAUDE.md` to `ARCHITECTURE.md`.
- Added basic pytest test suite.

### Removed
- `noisereduce` from the transcription hot path (and `requirements.txt`). Spectral-gate denoising on CPU added 1-3s per clip on a built-in MacBook mic where it was buying close to nothing.
- `soundfile` dependency (no longer needed once disk round-trip was removed).

### Fixed
- **Transcription failure silently killing dictation (issue #170).** A bad transcription could permanently wedge the app: `_do_transcribe` runs on a daemon thread, touches AppKit/rumps off the main thread, and cleared the `_transcribing` gate only as its last statement — so any raise (e.g. a `unknown format: 3` decode error on a format-3 IEEE-float WAV, observed 2026-06-26) left the gate stuck `True`, after which `_on_hotkey_press` ignored every future key-press. Three-part fix: (1) transcription now runs through `_safe_transcribe`, which clears the gate in a `finally` so a crash can never wedge it; (2) `_on_hotkey_press` force-recovers if the gate has been held longer than `TRANSCRIBE_STUCK_TIMEOUT` (30s), covering a true *hang* where `finally` never runs; (3) `_wav_bytes_to_float32` falls back to libsndfile (`soundfile`) for any WAV the stdlib `wave` module rejects, so non-PCM audio decodes instead of crashing. Regression-locked in `tests/test_transcribe_wedge.py`.
- **Hotkey going silently dead after a long hold (issue #156).** The CGEventTap callback ran on the main run loop and invoked the press/release handlers synchronously — `recorder.stop()` (closing the audio stream + assembling the WAV) could block it past macOS's tap timeout, after which macOS disabled the tap and a plain `CGEventTapEnable` never restored event delivery. Two fixes: (1) press/release handlers are now dispatched to worker threads so the tap callback always returns instantly; (2) the watchdog now *recreates* the tap (tears down the source and rebuilds it) when a re-enable doesn't stick, on a 5s interval instead of 30s. Witnessed via `tests/manual_selfheal.py`.
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
