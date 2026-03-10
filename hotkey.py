"""Global hotkey detection using CGEventTap.
Attaches to the MAIN thread's run loop (required for HID event delivery).
Only requires Accessibility permission.
"""

import os
import time
import Quartz

# Right Command key = keyCode 54
HOTKEY_KEYCODE = 54
# kCGEventFlagMaskCommand = 0x100000
HOTKEY_FLAG = 0x100000

# Debounce: ignore press/release cycles shorter than this (seconds)
MIN_HOLD_DURATION = 0.15
# Cooldown: minimum gap between end of one recording and start of next (seconds)
COOLDOWN_AFTER_RELEASE = 0.5

STATE_DIR = os.path.expanduser("~/.voice-clip")
LOG_PATH = os.path.join(STATE_DIR, "voiceclip-debug.log")


def _log(msg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        from datetime import datetime
        f.write(f"{datetime.now().isoformat()} [hotkey] {msg}\n")


class HotkeyListener:
    """Listens for Right Command key press/release using CGEventTap on the main run loop."""

    def __init__(self, on_press=None, on_release=None):
        self._on_press = on_press
        self._on_release = on_release
        self._key_down = False
        self._tap = None
        self._source = None
        self._watchdog = None
        self._press_time = 0.0
        self._last_release_time = 0.0

    def start(self):
        """Attach CGEventTap to the main thread's run loop.
        Must be called BEFORE the NSApplication run loop starts (i.e. before rumps.App.run()).
        """
        # Right Command is modifier-only, so flagsChanged is sufficient.
        mask = 1 << Quartz.kCGEventFlagsChanged
        listener = self

        def tap_callback(proxy, event_type, event, refcon):
            try:
                # If macOS sends kCGEventTapDisabledByTimeout, re-enable
                if event_type == Quartz.kCGEventTapDisabledByTimeout:
                    _log("CGEventTap was disabled by timeout — re-enabling")
                    if listener._tap:
                        Quartz.CGEventTapEnable(listener._tap, True)
                    return event

                if event_type == Quartz.kCGEventTapDisabledByUserInput:
                    _log("CGEventTap was disabled by user input — re-enabling")
                    if listener._tap:
                        Quartz.CGEventTapEnable(listener._tap, True)
                    return event

                # Right Command is a modifier — only handle flagsChanged events
                # to avoid duplicate firing from keyDown/keyUp events
                if event_type != Quartz.kCGEventFlagsChanged:
                    return event

                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode
                )
                flags = Quartz.CGEventGetFlags(event)

                if keycode == HOTKEY_KEYCODE:
                    key_down = bool(flags & HOTKEY_FLAG)
                    now = time.monotonic()
                    if key_down and not listener._key_down:
                        listener._key_down = True
                        # Cooldown check: ignore if too soon after last release
                        elapsed_since_release = now - listener._last_release_time
                        if elapsed_since_release < COOLDOWN_AFTER_RELEASE:
                            _log(f"Right Cmd PRESS ignored (cooldown: {elapsed_since_release:.3f}s < {COOLDOWN_AFTER_RELEASE}s)")
                        else:
                            listener._press_time = now
                            _log("Right Cmd PRESS detected")
                            if listener._on_press:
                                listener._on_press()
                    elif not key_down and listener._key_down:
                        listener._key_down = False
                        listener._last_release_time = now
                        # Debounce: ignore if held for less than minimum duration
                        hold_duration = now - listener._press_time
                        if listener._press_time == 0.0 or hold_duration < MIN_HOLD_DURATION:
                            _log(f"Right Cmd RELEASE ignored (hold: {hold_duration:.3f}s < {MIN_HOLD_DURATION}s)")
                        else:
                            _log(f"Right Cmd RELEASE detected (held {hold_duration:.3f}s)")
                            if listener._on_release:
                                listener._on_release()
            except Exception as e:
                _log(f"callback error: {e}")
            return event

        _log("Creating CGEventTap...")
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            tap_callback,
            None,
        )

        if tap is None:
            _log("FAILED to create CGEventTap — Accessibility permission not granted?")
            return False

        # Add to the MAIN run loop — this is key.
        # rumps/NSApplication will drive this run loop.
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        main_loop = Quartz.CFRunLoopGetMain()
        Quartz.CFRunLoopAddSource(main_loop, source, Quartz.kCFRunLoopDefaultMode)
        Quartz.CGEventTapEnable(tap, True)

        _log(f"CGEventTap attached to MAIN run loop: {tap}")
        # Keep references alive
        self._tap = tap
        self._source = source

        # Use a threading timer instead for the watchdog
        import threading

        def _watchdog_loop():
            import time
            while True:
                time.sleep(30)
                if listener._tap:
                    try:
                        enabled = Quartz.CGEventTapIsEnabled(listener._tap)
                        if not enabled:
                            _log("Watchdog: CGEventTap was disabled — re-enabling")
                            Quartz.CGEventTapEnable(listener._tap, True)
                    except Exception as e:
                        _log(f"Watchdog error: {e}")

        t = threading.Thread(target=_watchdog_loop, daemon=True)
        t.start()

        return True

    def stop(self):
        """Stop listening."""
        if hasattr(self, '_tap') and self._tap:
            Quartz.CGEventTapEnable(self._tap, False)
