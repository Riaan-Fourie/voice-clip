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
- **Solution: transcription runs through `_safe_transcribe`, which clears the flag in a `finally`.** Belt-and-suspenders: `_on_hotkey_press` force-resets the flag if it's been held past `TRANSCRIBE_STUCK_TIMEOUT` (30s).
- **But a flag reset alone does NOT recover a true *hang*** — see the next entry.

### A hang INSIDE the native transcribe call can't be unwedged in-process — re-exec (issue #187 / jarvis-system #6)
- On 2026-06-30 a transcription hung *inside* `mlx_whisper.transcribe` (native Metal) and never returned. The #170 backstop fired (`_transcribing stuck 556s — force-resetting`) and cleared the flag, but the app stayed functionally dead: every later press logged PRESS/RELEASE with **no `Transcribing` line** until a manual `kill`+restart.
- Why a reset isn't enough: Python can't kill the wedged worker thread, clearing the gate just piles the next recording behind the same stuck GPU queue, and a fresh `Transcriber` reuses `mlx_whisper`'s **process-global** model singleton. Nothing in-process clears a wedged Metal command queue.
- **Solution: a background `_transcribe_watchdog` re-execs the process (`os.execv`) when a transcription is stuck past the timeout AND its worker thread is still alive** (`_stuck_recovery_action() == "reexec"`). `execv` keeps the PID but discards all in-process state — the automatic equivalent of the manual kill+restart. A *leaked* gate (worker already dead) still gets the cheap flag reset; only a live wedge re-execs.
- The watchdog can run during a wedge **because a native Metal hang releases the GIL** (confirmed: the incident log still recorded hotkey events while transcription was wedged).
- **Self-relaunch via `execv`, NOT self-exit**, because there is deliberately no LaunchAgent — a LaunchAgent breaks the CGEventTap (see the run-mode gotcha). Self-exit would leave the app dead.
- The instance-lock fd is opened `O_CLOEXEC` so the exec releases the `flock` and the fresh image re-acquires it; without it the re-exec'd process would see the lock held and exit as a false "duplicate". Witnessed end-to-end by `tests/manual_reexec.py`.

### Whisper does not see only PCM — decode defensively
- The Recorder always writes 16-bit PCM, but a format-3 (IEEE-float) WAV reached the decoder on 2026-06-26. Python's stdlib `wave` module is PCM-only and raises `wave.Error: unknown format: 3` for it.
- **Solution: `_wav_bytes_to_float32` falls back to libsndfile (`soundfile`) for anything `wave` rejects** (lazy import — only the rare non-PCM path pays the cost). `soundfile` had been removed from `requirements.txt`; it's back for this.

## Audio

### AirPods mic flips the Bluetooth link to HFP (harsh audible clip)
- Opening ANY AirPods mic stream renegotiates A2DP -> HFP: playback crunches to
  call quality with a harsh clip, popping back ~1s after the mic closes
- OS/Bluetooth-level; no app can remove it (issue #190 was reverted over this)
- macOS 26 "studio-quality" AirPods recording does NOT reach plain CoreAudio
  streams — probed 2026-07-17: nominal rate stays 24kHz wideband, not 48kHz
- **Solution: Transition submenu (#264) masks the flip — Volume Fade (default)
  or Auto-Pause Music; zero overhead on non-Bluetooth mics**

### AirPods mic produces poor transcription
- Bluetooth SCO/HFP codec is low quality (8kHz mono)
- Whisper struggles with this audio quality
- Originally solved by hard-preferring the MacBook built-in mic
- **Superseded (Jarvis #263): Riaan accepts the trade-off for convenience — AirPods
  is now the default, with a Microphone submenu (persisted to
  `~/.voice-clip/settings.json`) to switch back to MacBook Mic / System Default
  in one click. If transcription quality drops noticeably, that toggle is the fix.**

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
