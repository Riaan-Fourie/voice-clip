"""Global hotkey detection using CGEventTap.
Attaches to the MAIN thread's run loop (required for HID event delivery).
Only requires Accessibility permission.

Resilience (see issue #156): the tap callback must NEVER do blocking work, or
macOS disables the tap by timeout. Press/release handlers are therefore
dispatched to worker threads, and a watchdog *recreates* the tap (not just
re-enables it) if it ever goes dead — a plain CGEventTapEnable does not restore
event delivery once the source is wedged.
"""

import threading
import time
import Quartz

from utils import _log as _log_base

# Right Command key = keyCode 54
HOTKEY_KEYCODE = 54
# kCGEventFlagMaskCommand = 0x100000
HOTKEY_FLAG = 0x100000

# Debounce: ignore press/release cycles shorter than this (seconds)
MIN_HOLD_DURATION = 0.15
# Cooldown: minimum gap between end of one recording and start of next (seconds)
COOLDOWN_AFTER_RELEASE = 0.5

# Watchdog: how often to check tap health (seconds). Kept short so a wedged tap
# recovers in seconds, not the ~30s it used to take.
WATCHDOG_INTERVAL = 5.0
# If the tap is still disabled after this many consecutive watchdog checks
# (i.e. a plain re-enable did not stick), tear it down and recreate it.
RECREATE_AFTER_FAILED_CHECKS = 2


def _log(msg):
    _log_base(msg, tag="hotkey")


class HotkeyListener:
    """Listens for Right Command key press/release using CGEventTap on the main run loop."""

    def __init__(self, on_press=None, on_release=None):
        self._on_press = on_press
        self._on_release = on_release
        self._key_down = False
        self._tap = None
        self._source = None
        self._mask = 1 << Quartz.kCGEventFlagsChanged
        self._tap_callback = None  # kept alive so the C callback isn't GC'd
        self._watchdog = None
        self._failed_checks = 0
        self._recreate_count = 0
        self._press_time = 0.0
        self._last_release_time = 0.0

    def _dispatch(self, fn):
        """Run a user handler off the tap callback thread.

        The CGEventTap callback runs on the main run loop; if a handler blocks
        (e.g. recorder.stop() closing the audio stream), macOS disables the tap
        by timeout. Off-loading keeps the callback instant. See issue #156.
        """
        if fn is None:
            return
        threading.Thread(target=fn, daemon=True).start()

    def _make_callback(self):
        """Build the CGEventTap C callback. Kept tiny and non-blocking:
        debounce/cooldown are cheap flag checks; the actual press/release
        handlers (which open/close the audio device) are dispatched to threads.
        """
        listener = self

        def tap_callback(proxy, event_type, event, refcon):
            try:
                # macOS disabled the tap — re-enable immediately. The watchdog
                # is the real backstop (it recreates the tap if this doesn't
                # stick), but handling it here recovers fast when the run loop
                # is still healthy.
                if event_type in (
                    Quartz.kCGEventTapDisabledByTimeout,
                    Quartz.kCGEventTapDisabledByUserInput,
                ):
                    _log(f"CGEventTap disabled (type={event_type}) — re-enabling in callback")
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
                            listener._dispatch(listener._on_press)
                    elif not key_down and listener._key_down:
                        listener._key_down = False
                        listener._last_release_time = now
                        # Debounce: ignore if held for less than minimum duration
                        hold_duration = now - listener._press_time
                        if listener._press_time == 0.0 or hold_duration < MIN_HOLD_DURATION:
                            _log(f"Right Cmd RELEASE ignored (hold: {hold_duration:.3f}s < {MIN_HOLD_DURATION}s)")
                        else:
                            _log(f"Right Cmd RELEASE detected (held {hold_duration:.3f}s)")
                            listener._dispatch(listener._on_release)
            except Exception as e:
                _log(f"callback error: {e}")
            return event

        return tap_callback

    def _create_tap(self):
        """Create the CGEventTap, attach it to the main run loop, enable it.
        Returns True on success. Safe to call repeatedly (used for recreation)."""
        _log("Creating CGEventTap...")
        # Keep a reference to the callback so it isn't garbage-collected while
        # the C side still holds it.
        self._tap_callback = self._make_callback()
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            self._mask,
            self._tap_callback,
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

        self._tap = tap
        self._source = source
        _log(f"CGEventTap attached to MAIN run loop: {tap}")
        return True

    def _teardown_tap(self):
        """Disable and detach the current tap + source so it can be recreated."""
        try:
            if self._tap:
                Quartz.CGEventTapEnable(self._tap, False)
            if self._source:
                main_loop = Quartz.CFRunLoopGetMain()
                Quartz.CFRunLoopRemoveSource(main_loop, self._source, Quartz.kCFRunLoopDefaultMode)
        except Exception as e:
            _log(f"teardown error: {e}")
        finally:
            self._tap = None
            self._source = None

    def _recreate_tap(self):
        """Fully rebuild the tap. Used when a plain re-enable fails to restore
        event delivery (the wedged-source case from issue #156)."""
        self._recreate_count += 1
        _log(f"Watchdog: recreating CGEventTap (recreation #{self._recreate_count})")
        self._teardown_tap()
        ok = self._create_tap()
        if ok:
            self._failed_checks = 0
            _log("Watchdog: CGEventTap recreated and re-enabled")
        else:
            _log("Watchdog: CGEventTap recreation FAILED — will retry")

    def start(self):
        """Attach CGEventTap to the main thread's run loop.
        Must be called BEFORE the NSApplication run loop starts (i.e. before rumps.App.run()).
        """
        if not self._create_tap():
            return False

        # Warm up lazily-bound Quartz symbols on this (main) thread so the
        # watchdog thread never trips an objc lazy-import race on first use.
        _ = Quartz.CGEventTapIsEnabled
        _ = Quartz.CFRunLoopRemoveSource

        def _watchdog_loop():
            while True:
                time.sleep(WATCHDOG_INTERVAL)
                if not self._tap:
                    continue
                try:
                    enabled = Quartz.CGEventTapIsEnabled(self._tap)
                except Exception as e:
                    _log(f"Watchdog error: {e}")
                    continue
                if enabled:
                    self._failed_checks = 0
                    continue
                # Tap is disabled. First try a cheap re-enable; if that doesn't
                # stick across checks, recreate the tap from scratch.
                self._failed_checks += 1
                _log(f"Watchdog: CGEventTap disabled (failed check {self._failed_checks}) — re-enabling")
                Quartz.CGEventTapEnable(self._tap, True)
                if self._failed_checks >= RECREATE_AFTER_FAILED_CHECKS:
                    self._recreate_tap()

        self._watchdog = threading.Thread(target=_watchdog_loop, daemon=True)
        self._watchdog.start()
        return True

    def stop(self):
        """Stop listening."""
        if hasattr(self, '_tap') and self._tap:
            Quartz.CGEventTapEnable(self._tap, False)
