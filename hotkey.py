"""Global hotkey detection using CGEventTap.
Attaches to the MAIN thread's run loop (required for HID event delivery).
Only requires Accessibility permission.

Resilience (see issue #156): the tap callback must NEVER do blocking work, or
macOS disables the tap by timeout. Press/release handlers are therefore
dispatched to worker threads, and a watchdog *recreates* the tap (not just
re-enables it) if it ever goes dead — a plain CGEventTapEnable does not restore
event delivery once the source is wedged.

Modifier state is recomputed from every flagsChanged event rather than tracked
as an up/down tally (see issue #290): a tally only stays correct if every single
up-event is delivered, and one lost up-event latched a modifier "down" for the
life of the process — silently promoting every hold to a locked recording.
"""

import threading
import time
import Quartz

from utils import _log as _log_base

# Right Command key = keyCode 54
HOTKEY_KEYCODE = 54
# kCGEventFlagMaskCommand = 0x100000
HOTKEY_FLAG = 0x100000

# Right Shift key = keyCode 60; pressing it WITH Right Command forms the
# hands-free lock toggle. kCGEventFlagMaskShift = 0x20000.
LOCK_KEYCODE = 60
LOCK_FLAG = 0x20000

# Device-dependent modifier bits (IOKit NX_DEVICE*KEYMASK). The generic masks
# above are set by EITHER key of a pair, so they cannot tell left from right:
# with Left-Cmd held, the Right-Cmd *up* event still carries 0x100000 and reads
# as "still down". These bits are per-physical-key, so the true state of one key
# can be read off any flagsChanged event regardless of what else is held (#290).
NX_DEVICE_LSHIFT = 0x000002
NX_DEVICE_RSHIFT = 0x000004
NX_DEVICE_LCMD = 0x000008
NX_DEVICE_RCMD = 0x000010
# Both device bits of each pair — used to decide whether an event carries device
# information at all before trusting it (see _physical_key_down).
NX_DEVICE_SHIFT_PAIR = NX_DEVICE_LSHIFT | NX_DEVICE_RSHIFT
NX_DEVICE_CMD_PAIR = NX_DEVICE_LCMD | NX_DEVICE_RCMD


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


def _physical_key_down(flags, device_mask, pair_mask, generic_mask):
    """True if one specific physical modifier key is down, per `flags`.

    Prefers the device-dependent bit, which is authoritative even when the other
    key of the pair is held. Falls back to the generic mask when the event
    carries no device bits for that pair at all — synthetic events (and some
    remappers) set only the generic mask, and those must keep working exactly as
    they did before #290.
    """
    if flags & pair_mask:
        return bool(flags & device_mask)
    return bool(flags & generic_mask)


def _log(msg):
    _log_base(msg, tag="hotkey")


class HotkeyListener:
    """Listens for Right Command key press/release using CGEventTap on the main run loop."""

    def __init__(self, on_press=None, on_release=None, on_toggle=None,
                 on_cancel=None):
        self._on_press = on_press
        self._on_release = on_release
        self._on_toggle = on_toggle
        self._on_cancel = on_cancel
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
        # Lock-toggle chord state (Right Shift + Right Command held together).
        self._rcmd_down = False
        self._rshift_down = False
        self._chord_latched = False   # so one chord fires on_toggle exactly once

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

                # Recompute both physical keys from THIS event's flags, whatever
                # key actually moved. flagsChanged carries the full current
                # modifier state, so this is a correction as well as an update:
                # a dropped up-event self-heals on the next modifier keystroke
                # instead of latching for the process lifetime (#290).
                listener._resync_modifiers(flags)

                if keycode == HOTKEY_KEYCODE:
                    listener._process_rcmd_edge()
                    listener._check_lock_chord()
                elif keycode == LOCK_KEYCODE:
                    # Right Shift changed — only relevant for the lock chord.
                    listener._check_lock_chord()
                else:
                    # Some other modifier moved. Nothing to trigger, but the
                    # resync above may have just corrected a stale Right-Cmd or
                    # Right-Shift — settle the resulting edge/latch so a lost
                    # up-event cannot leave a recording running or the chord
                    # armed (#290). Release-only: a recording must always be
                    # able to stop, but must never START off a key the user
                    # did not actually press.
                    listener._process_rcmd_edge(allow_press=False)
                    listener._check_lock_chord()
            except Exception as e:
                _log(f"callback error: {e}")
            return event

        return tap_callback

    def _resync_modifiers(self, flags):
        """Recompute Right-Cmd / Right-Shift physical state from an event's flags.

        Authoritative rather than incremental: flagsChanged events carry the full
        current modifier state, so this both updates and *corrects* the tracked
        state. That is what makes a lost up-event survivable — the old code only
        touched a key's flag inside its own keycode branch, so one missed
        Right-Shift release latched `_rshift_down` True forever and every
        subsequent Right-Cmd hold was promoted to a locked recording (#290).
        """
        self._rcmd_down = _physical_key_down(
            flags, NX_DEVICE_RCMD, NX_DEVICE_CMD_PAIR, HOTKEY_FLAG
        )
        self._rshift_down = _physical_key_down(
            flags, NX_DEVICE_RSHIFT, NX_DEVICE_SHIFT_PAIR, LOCK_FLAG
        )

    def _process_rcmd_edge(self, allow_press=True):
        """Turn the resynced Right-Cmd state into press/release handler calls.

        Debounce and cooldown are unchanged; only the source of truth moved from
        an incremental tally to `_rcmd_down`. `allow_press=False` restricts this
        to the release edge, used when some *other* modifier moved: correcting a
        missed release there is a safety net, but firing a press would start a
        recording off a key the user never touched.
        """
        key_down = self._rcmd_down
        now = time.monotonic()
        if key_down and not self._key_down:
            if not allow_press:
                return
            self._key_down = True
            # Cooldown check: ignore if too soon after last release
            elapsed_since_release = now - self._last_release_time
            if elapsed_since_release < COOLDOWN_AFTER_RELEASE:
                _log(f"Right Cmd PRESS ignored (cooldown: {elapsed_since_release:.3f}s < {COOLDOWN_AFTER_RELEASE}s)")
            else:
                self._press_time = now
                _log("Right Cmd PRESS detected")
                self._dispatch(self._on_press)
        elif not key_down and self._key_down:
            self._key_down = False
            self._last_release_time = now
            # Debounce: ignore if held for less than minimum duration
            hold_duration = now - self._press_time
            if self._press_time == 0.0 or hold_duration < MIN_HOLD_DURATION:
                # The press already opened the mic, so swallowing the release
                # here strands the recorder in the recording state — it then
                # captures room audio until some later, unrelated release clears
                # the threshold, and the whole buffer transcribes as one stray
                # fragment (#327: 27 runaways, median 25s, worst 217s).
                # Debounce still means "this tap was an accident", so cancel
                # rather than transcribe — but it must never mean "do nothing".
                _log(f"Right Cmd RELEASE too short (hold: {hold_duration:.3f}s < {MIN_HOLD_DURATION}s) — cancelling")
                self._dispatch(self._on_cancel)
            else:
                _log(f"Right Cmd RELEASE detected (held {hold_duration:.3f}s)")
                self._dispatch(self._on_release)

    def _check_lock_chord(self):
        """Fire on_toggle once when Right Shift + Right Command are both held.

        Latched so a single chord fires exactly one toggle; the latch clears when
        either key lifts, so the next chord fires again. Kept tiny + non-blocking
        (the handler is dispatched to a worker thread) so the tap never stalls."""
        if self._rcmd_down and self._rshift_down:
            if not self._chord_latched:
                self._chord_latched = True
                _log("Right Shift+Cmd LOCK chord detected")
                self._dispatch(self._on_toggle)
        else:
            self._chord_latched = False

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
