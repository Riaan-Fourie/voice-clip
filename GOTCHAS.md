# VoiceClip — Gotchas & Lessons Learned

## macOS TCC (Transparency, Consent, Control)

### Apple SFSpeechRecognizer crashes from Python
- Apple's Speech framework requires `NSSpeechRecognitionUsageDescription` in the calling app's Info.plist
- When running via `python main.py`, the "app" is Python.app (a trampoline binary inside the Python framework)
- Python.app's Info.plist doesn't have speech recognition keys → hard TCC crash (SIGKILL)
- **Patching Python.app's Info.plist breaks its code signature** → macOS still rejects it
- `codesign --force --deep --sign -` to re-sign doesn't help — the trampoline delegates to the framework Python
- `tccutil reset SpeechRecognition org.python.python` doesn't help either
- Creating a .app bundle wrapper doesn't work because the Python binary is a trampoline that delegates to framework Python.app — TCC still checks that binary's plist
- **Solution: Don't use Apple Speech from Python. Use mlx-whisper instead (no TCC needed).**

## Hotkey Detection

### Fn key is unusable on macOS
- Fn is intercepted by macOS system (triggers dictation, emoji picker, Spotlight depending on settings)
- CGEventTap never sees Fn events — they're consumed before reaching user-space event taps
- **Solution: Use Right Command key instead**

### pynput requires Input Monitoring permission
- pynput uses `CGEventTapCreate` internally but macOS classifies it as Input Monitoring, not Accessibility
- User rejected granting Input Monitoring (too broad)
- **Solution: Use CGEventTap directly via Quartz — only needs Accessibility permission**

### NSEvent.addGlobalMonitorForEvents doesn't fire for modifier-only keys
- Only fires for "complete" key events (keyDown/keyUp), not flagsChanged for modifiers pressed alone
- **Solution: CGEventTap with kCGEventFlagsChanged mask**

### CGEventTap must be on main thread's run loop
- If attached to a background thread's run loop, HID events are never delivered
- Must call `CGEventTapCreate` + `CFRunLoopAddSource` on the main thread BEFORE `NSApplication.run()`
- rumps.App.run() starts the NSApplication run loop, so attach the tap in __init__ before .run()

### NEVER do blocking work in the tap callback (issue #156)
- The tap callback runs on the main run loop. If it blocks longer than macOS's tap timeout (~1s), macOS disables the tap.
- We hit this because the release handler called `recorder.stop()` (closing the PortAudio stream + assembling a 31s WAV) synchronously inside the callback. A long hold blew the timeout, the tap was disabled, and the app went silently dead while still "running".
- **Solution: the callback only does cheap flag/debounce checks; press/release handlers are dispatched to worker threads (`HotkeyListener._dispatch`).**

### A disabled tap may not recover from `CGEventTapEnable` alone — recreate it
- Once the tap/source is wedged (run loop stops delivering events to it), re-enabling with `CGEventTapEnable(tap, True)` does NOT restore delivery — the old watchdog looped on this forever.
- **Solution: the watchdog tears down the source (`CFRunLoopRemoveSource`) and rebuilds the tap from scratch (`_recreate_tap`) when a re-enable doesn't stick after a couple of checks.** When the run loop IS healthy, the in-callback re-enable handles transient disables instantly; recreation is the backstop for the wedged case.

### Lazily-bound Quartz symbols can race when first touched from a background thread
- `Quartz.CGEventTapIsEnabled` is only used by the watchdog thread. The first access went through pyobjc's lazy import and intermittently raised `KeyError` from a background thread.
- **Solution: "warm up" such symbols by referencing them once on the main thread in `start()` before spawning the watchdog.**

## Transcription pipeline

### A raised exception in the transcribe thread can wedge dictation forever (issue #170)
- `_do_transcribe` runs on a daemon thread, mutates AppKit/rumps UI off the main thread, and the app gates new recordings on a `self._transcribing` flag.
- The flag was cleared only as the *last statement* of `_do_transcribe`. If anything before it raised — a `unknown format: 3` WAV decode error, a `rumps.notification` failure, any off-main-thread UI call — the flag stayed `True`, and `_on_hotkey_press` then silently ignored every future key-press. The app looks alive (icon present, CGEventTap healthy) but does nothing. This is a *different* failure from the #156 tap-wedge.
- **Solution: transcription runs through `_safe_transcribe`, which clears the flag in a `finally`.** Belt-and-suspenders: `_on_hotkey_press` force-resets the flag if it's been held past `TRANSCRIBE_STUCK_TIMEOUT` (30s), which also covers a true *hang* where `finally` never runs.

### Whisper does not see only PCM — decode defensively
- The Recorder always writes 16-bit PCM, but a format-3 (IEEE-float) WAV reached the decoder on 2026-06-26. Python's stdlib `wave` module is PCM-only and raises `wave.Error: unknown format: 3` for it.
- **Solution: `_wav_bytes_to_float32` falls back to libsndfile (`soundfile`) for anything `wave` rejects** (lazy import — only the rare non-PCM path pays the cost). `soundfile` had been removed from `requirements.txt`; it's back for this.

## Audio

### AirPods mic produces poor transcription
- Bluetooth SCO/HFP codec is low quality (8kHz mono)
- Whisper struggles with this audio quality
- **Solution: Always prefer MacBook Air built-in mic (device lookup by name)**

### VU meter sensitivity
- int16 audio RMS for normal speech at MacBook mic distance: ~200-2000
- Dividing by 8000 makes bars barely move at normal volume
- **Solution: Divide by 1500 for good sensitivity at normal speaking distance**

## Overlay / Window

### NSWindow cannot appear above fullscreen apps
- Even with high window levels, NSWindow from a non-fullscreen app won't render on fullscreen spaces
- `NSWindowCollectionBehaviorCanJoinAllSpaces` alone is not enough
- **Solution: Use NSPanel with NSWindowStyleMaskNonactivatingPanel + setFloatingPanel_(True) + setHidesOnDeactivate_(False)**
- Also use `orderFrontRegardless()` instead of `orderFront_(None)`
- Collection behavior: `canJoinAllSpaces | fullScreenAuxiliary | stationary`

## LaunchAgent

### NEVER use launchctl/LaunchAgent to start VoiceClip
- Launching via launchd runs the process under a **different macOS security context**
- Accessibility permission granted to Terminal/Python does NOT carry over to launchd-spawned processes
- CGEventTap gets silently disabled by macOS immediately (every 30s the watchdog would re-enable it, macOS would disable it again)
- This completely breaks hotkey detection — the app appears to run fine but never receives key events
- **Solution: Always start VoiceClip directly from Terminal** (`./venv/bin/python main.py &disown`)
- For auto-start, consider Login Items (System Settings > General > Login Items) pointing to the .app bundle instead of launchd

## Packaging

### Python.app trampoline problem
- The `python3` binary in a Homebrew/framework install is a small C trampoline
- It calls `posix_spawn` to launch the real Python from `Python.framework/.../Python.app/Contents/MacOS/Python`
- This means any .app bundle wrapping `python3` inherits the framework Python.app's identity for TCC purposes
- **Cannot use Python.app as a proper macOS .app for TCC-gated features**
