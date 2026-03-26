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
- **mlx-whisper** running locally on Apple Silicon Metal GPU
- Model: `mlx-community/whisper-base-mlx` (~150MB, good speed/accuracy balance)
- Lazy model loading on first transcription (warm-up with a silence WAV)
- Failed recordings saved to `~/.voice-clip/failed/` with error metadata for retry
- Thread-safe initialization via `threading.Lock`

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
│   └── mlx_whisper.transcribe() → clipboard copy → simulated Cmd+V paste
├── Retry thread (_do_retry)
├── Hotkey watchdog (re-enables CGEventTap every 30s)
└── Timer threads (3s status reset after transcription)
```

Key constraint: all AppKit/NSPanel operations must run on the main thread. The overlay dispatches via `AppHelper.callAfter` when called from background threads.

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
