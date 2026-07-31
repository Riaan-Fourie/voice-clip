"""Modifier-state resync (issue #290).

The bug: modifier state was tracked as an incremental up/down tally, updated only
inside each key's own keycode branch. One lost Right-Shift up-event therefore
latched `_rshift_down` True for the life of the process, so every subsequent
Right-Cmd hold fired the lock chord and was silently promoted to a *locked*
recording — releasing the key no longer stopped it. Live symptom: 66.3s of audio
captured for a ~15s utterance.

Pure logic, no macOS APIs except a stubbed Quartz for the callback test.
"""
import time
from unittest import mock

import pytest

import hotkey
from hotkey import (
    HOTKEY_FLAG,
    HOTKEY_KEYCODE,
    LOCK_FLAG,
    LOCK_KEYCODE,
    MIN_HOLD_DURATION,
    NX_DEVICE_CMD_PAIR,
    NX_DEVICE_LCMD,
    NX_DEVICE_LSHIFT,
    NX_DEVICE_RCMD,
    NX_DEVICE_RSHIFT,
    NX_DEVICE_SHIFT_PAIR,
    _physical_key_down,
)


@pytest.fixture
def hk():
    """A listener whose handlers run inline instead of on worker threads."""
    listener = hotkey.HotkeyListener(
        on_press=mock.Mock(), on_release=mock.Mock(), on_toggle=mock.Mock()
    )
    listener._dispatch = lambda fn: fn() if fn else None
    return listener


# --- _physical_key_down: left/right disambiguation -------------------------


class TestPhysicalKeyDown:
    def test_device_bit_wins_over_generic(self):
        """THE BUG: Left-Cmd held makes the generic Cmd mask set even though
        Right-Cmd is up. The device bit must call it up."""
        flags = HOTKEY_FLAG | NX_DEVICE_LCMD  # left cmd down, right cmd up
        assert not _physical_key_down(
            flags, NX_DEVICE_RCMD, NX_DEVICE_CMD_PAIR, HOTKEY_FLAG
        )

    def test_right_key_down_reads_true(self):
        flags = HOTKEY_FLAG | NX_DEVICE_RCMD
        assert _physical_key_down(
            flags, NX_DEVICE_RCMD, NX_DEVICE_CMD_PAIR, HOTKEY_FLAG
        )

    def test_both_keys_down_reads_true(self):
        flags = HOTKEY_FLAG | NX_DEVICE_LCMD | NX_DEVICE_RCMD
        assert _physical_key_down(
            flags, NX_DEVICE_RCMD, NX_DEVICE_CMD_PAIR, HOTKEY_FLAG
        )

    def test_left_shift_held_masks_right_shift_release(self):
        """Same ambiguity on the shift pair — the other half of #290."""
        flags = LOCK_FLAG | NX_DEVICE_LSHIFT
        assert not _physical_key_down(
            flags, NX_DEVICE_RSHIFT, NX_DEVICE_SHIFT_PAIR, LOCK_FLAG
        )

    def test_falls_back_to_generic_without_device_bits(self):
        """Synthetic events carry only the generic mask — keep old behaviour."""
        assert _physical_key_down(
            HOTKEY_FLAG, NX_DEVICE_RCMD, NX_DEVICE_CMD_PAIR, HOTKEY_FLAG
        )

    def test_nothing_held_reads_false(self):
        assert not _physical_key_down(
            0, NX_DEVICE_RCMD, NX_DEVICE_CMD_PAIR, HOTKEY_FLAG
        )


# --- _resync_modifiers: correction, not accumulation -----------------------


class TestResync:
    def test_clears_a_latched_shift(self, hk):
        """THE REGRESSION TEST. A stale `_rshift_down` must be corrected by the
        next event, not persist for the process lifetime."""
        hk._rshift_down = True  # latched by a lost up-event
        hk._resync_modifiers(NX_DEVICE_RCMD | HOTKEY_FLAG)  # only right-cmd down
        assert hk._rshift_down is False
        assert hk._rcmd_down is True

    def test_latched_shift_no_longer_fires_chord_on_plain_cmd(self, hk):
        """End-to-end shape of the live bug: with a latched shift, a plain
        Right-Cmd press used to fire the lock chord and promote to locked."""
        hk._rshift_down = True
        hk._resync_modifiers(NX_DEVICE_RCMD | HOTKEY_FLAG)
        hk._check_lock_chord()
        hk._on_toggle.assert_not_called()

    def test_real_chord_still_fires_once(self, hk):
        flags = NX_DEVICE_RCMD | HOTKEY_FLAG | NX_DEVICE_RSHIFT | LOCK_FLAG
        hk._resync_modifiers(flags)
        hk._check_lock_chord()
        hk._check_lock_chord()  # latched — still one toggle
        hk._on_toggle.assert_called_once()

    def test_chord_rearms_after_release(self, hk):
        chord = NX_DEVICE_RCMD | HOTKEY_FLAG | NX_DEVICE_RSHIFT | LOCK_FLAG
        hk._resync_modifiers(chord)
        hk._check_lock_chord()
        hk._resync_modifiers(NX_DEVICE_RCMD | HOTKEY_FLAG)  # shift lifted
        hk._check_lock_chord()
        hk._resync_modifiers(chord)
        hk._check_lock_chord()
        assert hk._on_toggle.call_count == 2


# --- _process_rcmd_edge: press/release edges -------------------------------


class TestRcmdEdge:
    def test_press_then_release(self, hk):
        hk._resync_modifiers(NX_DEVICE_RCMD | HOTKEY_FLAG)
        hk._process_rcmd_edge()
        hk._on_press.assert_called_once()

        hk._press_time = time.monotonic() - 1.0  # past the debounce window
        hk._resync_modifiers(0)
        hk._process_rcmd_edge()
        hk._on_release.assert_called_once()

    def test_short_tap_is_debounced(self, hk):
        hk._resync_modifiers(NX_DEVICE_RCMD | HOTKEY_FLAG)
        hk._process_rcmd_edge()
        hk._resync_modifiers(0)
        hk._process_rcmd_edge()  # released well within MIN_HOLD_DURATION
        hk._on_release.assert_not_called()

    def test_missed_release_recovers_on_unrelated_modifier(self, hk):
        """A lost Right-Cmd up-event left a recording running forever. Any later
        modifier event must be able to settle it (release-only path)."""
        hk._resync_modifiers(NX_DEVICE_RCMD | HOTKEY_FLAG)
        hk._process_rcmd_edge()
        hk._on_press.assert_called_once()
        hk._press_time = time.monotonic() - 1.0

        # Right-Cmd up-event never arrived; user presses Option. Flags show the
        # true state: right-cmd is up.
        hk._resync_modifiers(0x080000)
        hk._process_rcmd_edge(allow_press=False)
        hk._on_release.assert_called_once()

    def test_unrelated_modifier_never_starts_a_recording(self, hk):
        """The release-only guard: a press must come from a real Right-Cmd
        event, never from a resync on someone else's keystroke."""
        hk._resync_modifiers(NX_DEVICE_RCMD | HOTKEY_FLAG)
        hk._process_rcmd_edge(allow_press=False)
        hk._on_press.assert_not_called()
        assert hk._key_down is False


# --- callback wiring: resync runs on EVERY flagsChanged event --------------


class TestCallbackResync:
    @staticmethod
    def _quartz_stub(keycode, flags):
        q = mock.Mock()
        q.kCGEventFlagsChanged = 12
        q.kCGEventTapDisabledByTimeout = 14
        q.kCGEventTapDisabledByUserInput = 15
        q.kCGKeyboardEventKeycode = 9
        q.CGEventGetIntegerValueField.return_value = keycode
        q.CGEventGetFlags.return_value = flags
        return q

    def test_latched_shift_cleared_by_an_unrelated_key(self, hk):
        """Proves the self-heal reaches the real callback path: a latched shift
        is corrected by a keystroke that is neither Cmd nor Shift."""
        hk._rshift_down = True
        q = self._quartz_stub(keycode=58, flags=0x080000)  # Option
        with mock.patch.object(hotkey, "Quartz", q):
            cb = hk._make_callback()
            cb(None, q.kCGEventFlagsChanged, object(), None)
        assert hk._rshift_down is False
        hk._on_toggle.assert_not_called()

    def test_plain_right_cmd_press_does_not_toggle(self, hk):
        """The user-visible fix: Right-Cmd alone starts a HOLD, never a lock."""
        hk._rshift_down = True  # pre-existing latch
        q = self._quartz_stub(
            keycode=HOTKEY_KEYCODE, flags=NX_DEVICE_RCMD | HOTKEY_FLAG
        )
        with mock.patch.object(hotkey, "Quartz", q):
            cb = hk._make_callback()
            cb(None, q.kCGEventFlagsChanged, object(), None)
        hk._on_press.assert_called_once()
        hk._on_toggle.assert_not_called()
