# VoiceClip Architecture

## Module Breakdown

### main.py — Application Entry Point
- **rumps** menubar app (tray icon with status indicators)
- Wires together all components: hotkey listener, recorder, transcriber, overlay
- Auto-paste via simulated `Cmd+V` (Quartz `CGEventCreateKeyboardEvent`)
- Single-instance enforcement using advisory file lock (`fcntl.flock`)
- Failed transcription retry via menubar action
- macOS notifications for transcription results

### hotkey.py — Global Hotkey Detection
- **CGEventTap** on the main thread's run loop for Right Command key (keycode 54)
- Listens to `kCGEventFlagsChanged` events (modifier-only keys don't produce keyDown/keyUp)
- Debounce: ignores press/release cycles shorter than 150ms (`MIN_HOLD_DURATION`)
- Cooldown: enforces 500ms gap between end of one recording and start of next (`COOLDOWN_AFTER_RELEASE`)
- Watchdog thread re-enables the tap every 30s if macOS disables it (timeout or user input events)
- Requires only **Accessibility** permission (not Input Monitoring)

### recorder.py — Audio Capture
- **sounddevice** `InputStream` capturing 16kHz mono int16 audio
- Prefers MacBook built-in mic over Bluetooth (device lookup by name)
- RMS level callback for VU meter (normalized to 0.0–1.0 via `/1500` divisor)
- Returns WAV bytes on `stop()` using the `wave` module

### transcriber.py — Speech-to-Text
- **Parakeet TDT 0.6B v2** via `parakeet-mlx` on the Apple Silicon GPU, quantised to 8 bits at load (Jarvis #769; was mlx-whisper)
- WAV bytes decoded in memory and fed straight to `get_logmel` + `generate`; no temp file, no ffmpeg
- Lazy model load on first transcription, warmed with 1 s of silence; `mx.clear_cache()` after every clip
- Proper nouns restored after transcription by `names.py` (joins + `replacements.txt`), since Parakeet takes no prompt
- Failed recordings saved to `~/.voice-clip/failed/` with error metadata for retry

### overlay.py — VU Meter Overlay
- **NSPanel** (not NSWindow) with `NonactivatingPanel` style — floats above fullscreen apps
- Custom `NSView` subclass draws animated audio level bars (8 bars, 30fps via `NSTimer`)
- Color gradient: green (low) → yellow (mid) → red (high)
- Collection behavior: `canJoinAllSpaces | fullScreenAuxiliary | stationary`
- Show/hide dispatched to main thread via `AppHelper.callAfter`

### utils.py — Shared Utilities
- Shared `_log()` function used by all modules for direct file logging

## Design Decisions

### CGEventTap over pynput / NSEvent
- **pynput** requires Input Monitoring permission (broader than needed)
- **NSEvent.addGlobalMonitorForEvents** doesn't fire for modifier-only keys (no `flagsChanged`)
- **CGEventTap** with `kCGEventFlagsChanged` mask works with Accessibility permission only
- Must be attached to the **main thread's** run loop — HID events are not delivered to background thread run loops

### NSPanel over NSWindow
- NSWindow from a non-fullscreen app cannot render on fullscreen Spaces
- NSPanel with `NonactivatingPanel` + `setFloatingPanel_(True)` + `setHidesOnDeactivate_(False)` works
- `orderFrontRegardless()` ensures visibility regardless of app activation state

### Auto Mic (Jarvis #767)
- **Microphone → Auto** picks the mic for the *next* dictation from a room check on the MacBook mic (`room_check.py`)
- Score = loudest 50 ms frames (95th pct) over quietest (20th pct), in dB; below 10 dB → AirPods, above 15 dB → MacBook mic, in between → no change
- The MacBook mic is always the probe: it is the main stream on a MacBook recording and a second stream on an AirPods one; the AirPods are never opened just to measure (HFP flip, #269)
- Runs only after a transcription found speech, so a silent hold cannot read as a noisy room

### Built-in Mic Preference
- Bluetooth SCO/HFP codec is low quality (8kHz mono) — Whisper struggles with it
- MacBook Air built-in mic at normal speaking distance produces good 16kHz audio
- Device selected by name match (`"MacBook"` in device name)

### mlx-whisper over Apple SFSpeechRecognizer
- Apple Speech requires `NSSpeechRecognitionUsageDescription` in the calling app's Info.plist
- Running from `python main.py`, the "app" is Python.app — a framework trampoline whose Info.plist cannot be safely patched (breaks code signature)
- mlx-whisper runs 100% locally on Metal GPU with zero TCC requirements

### Right Command over Fn
- Fn is intercepted by macOS for dictation/emoji picker — CGEventTap never sees it
- Right Command is rarely used and reliably detected via `kCGEventFlagsChanged`

## Threading Model

```
Main Thread (NSApplication run loop)
├── CGEventTap callback (hotkey.py)
│   ├── on_press  → starts recorder, shows overlay
│   └── on_release → stops recorder, hides overlay, spawns transcription thread
├── NSTimer (30fps) → overlay bar animation
├── AppHelper.callAfter → overlay show/hide dispatch
└── rumps event handling (menubar clicks, notifications)

Background Threads (daemon)
├── Transcription thread (_do_transcribe)
│   └── mlx_whisper.transcribe() → clipboard write (hops to main) → simulated Cmd+V paste
├── Retry thread (_do_retry)
├── Hotkey watchdog (re-enables CGEventTap every 30s)
└── Timer threads (3s status reset after transcription)
```

Key constraint: all AppKit/NSPanel operations must run on the main thread. The overlay dispatches via `AppHelper.callAfter` when called from background threads. The clipboard write does the same and waits for it, because the paste right after reads the clipboard (`_copy_to_clipboard`, #629).

## Build / Release

### Development
```bash
./install.sh          # Create venv, install deps, set up permissions
./start.sh            # Run from venv
```

### Uninstall
```bash
./install.sh --uninstall
```

### App Bundle
- `VoiceClip.app` in repo root (Login Items auto-start target)
- PyInstaller spec: `VoiceClip.spec`
- py2app setup: `setup_app.py`

### Runtime Files
- Logs: `~/.voice-clip/voiceclip-debug.log`
- Failed recordings: `~/.voice-clip/failed/`
- PID lock: `~/.voice-clip/voiceclip.pid`
