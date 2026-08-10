"""Sub-threshold tap must cancel, never strand the recorder (issue #327).

The bug: the debounce rejected the *release* edge, but the press edge had already
opened the mic. A Right-Cmd tap shorter than MIN_HOLD_DURATION therefore started a
recording that nothing stopped — it ran until some later, unrelated release
cleared the threshold, and the whole buffer (median 25s, worst 217s in the live
log) transcribed as one stray fragment. Read as "VoiceClip truncated my dictation".

Pure logic, no macOS APIs.
"""
import time
from unittest import mock

import pytest

import hotkey
from hotkey import (
    HOTKEY_FLAG,
    MIN_HOLD_DURATION,
    NX_DEVICE_RCMD,
)

DOWN = HOTKEY_FLAG | NX_DEVICE_RCMD  # right cmd physically down
UP = 0                               # nothing held


@pytest.fixture
def hk():
    """A listener whose handlers run inline instead of on worker threads."""
    listener = hotkey.HotkeyListener(
        on_press=mock.Mock(),
        on_release=mock.Mock(),
        on_toggle=mock.Mock(),
        on_cancel=mock.Mock(),
    )
    listener._dispatch = lambda fn: fn() if fn else None
    # Clear the cooldown so the first press in each test is always accepted.
    listener._last_release_time = -1000.0
    return listener


def _tap(hk, hold_seconds):
    """Drive one full press/release gesture of the given duration."""
    hk._resync_modifiers(DOWN)
    hk._process_rcmd_edge()
    hk._press_time = time.monotonic() - hold_seconds  # simulate the hold
    hk._resync_modifiers(UP)
    hk._process_rcmd_edge()


class TestSubThresholdTap:
    def test_short_tap_starts_a_recording(self, hk):
        """The press fires regardless of how briefly the key is held — this is
        what makes swallowing the release dangerous."""
        _tap(hk, 0.05)
        hk._on_press.assert_called_once()

    def test_short_tap_cancels_instead_of_being_swallowed(self, hk):
        """THE BUG: this used to dispatch nothing at all, leaving the mic open."""
        _tap(hk, 0.05)
        hk._on_cancel.assert_called_once()

    def test_short_tap_does_not_transcribe(self, hk):
        """An accidental tap is not speech — it must never reach transcription."""
        _tap(hk, 0.05)
        hk._on_release.assert_not_called()

    @pytest.mark.parametrize("hold", [0.018, 0.042, 0.079, 0.113, 0.145])
    def test_every_observed_runaway_hold_now_cancels(self, hk, hold):
        """Hold durations taken from the real runaways in the live debug log."""
        _tap(hk, hold)
        hk._on_cancel.assert_called_once()
        hk._on_release.assert_not_called()

    def test_gesture_never_ends_with_both_handlers_silent(self, hk):
        """The invariant: once a press has opened the mic, the release edge must
        dispatch *something* that closes it."""
        _tap(hk, 0.05)
        assert hk._on_release.called or hk._on_cancel.called


class TestNormalHoldUnchanged:
    def test_normal_hold_transcribes(self, hk):
        _tap(hk, MIN_HOLD_DURATION + 0.5)
        hk._on_release.assert_called_once()

    def test_normal_hold_does_not_cancel(self, hk):
        _tap(hk, MIN_HOLD_DURATION + 0.5)
        hk._on_cancel.assert_not_called()

    def test_long_hold_transcribes(self, hk):
        _tap(hk, 30.0)
        hk._on_release.assert_called_once()
        hk._on_cancel.assert_not_called()


class TestBackwardCompatibility:
    def test_listener_without_on_cancel_still_works(self):
        """on_cancel is optional; a listener built the old way must not raise."""
        listener = hotkey.HotkeyListener(
            on_press=mock.Mock(), on_release=mock.Mock(), on_toggle=mock.Mock()
        )
        listener._dispatch = lambda fn: fn() if fn else None
        listener._last_release_time = -1000.0
        _tap(listener, 0.05)  # must not raise on the None cancel handler
        listener._on_release.assert_not_called()
