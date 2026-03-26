"""Tests for hotkey module — pure logic only, no macOS APIs."""

import pytest

from hotkey import MIN_HOLD_DURATION, COOLDOWN_AFTER_RELEASE, HOTKEY_KEYCODE, HOTKEY_FLAG


class TestHotkeyConstants:
    """Verify hotkey configuration constants are sensible."""

    def test_right_command_keycode(self):
        """Right Command is keycode 54 on macOS."""
        assert HOTKEY_KEYCODE == 54

    def test_command_flag_mask(self):
        """kCGEventFlagMaskCommand = 0x100000."""
        assert HOTKEY_FLAG == 0x100000

    def test_min_hold_duration_positive(self):
        """Debounce threshold must be positive."""
        assert MIN_HOLD_DURATION > 0

    def test_min_hold_duration_reasonable(self):
        """Debounce should be short enough to not feel laggy (< 500ms)."""
        assert MIN_HOLD_DURATION < 0.5

    def test_cooldown_positive(self):
        """Cooldown must be positive."""
        assert COOLDOWN_AFTER_RELEASE > 0

    def test_cooldown_reasonable(self):
        """Cooldown should be under 2s to avoid feeling unresponsive."""
        assert COOLDOWN_AFTER_RELEASE < 2.0


class TestDebounceLogic:
    """Test the debounce/cooldown decision logic extracted from the callback."""

    @staticmethod
    def should_accept_press(elapsed_since_release: float) -> bool:
        """Mirrors the cooldown check in the CGEventTap callback."""
        return elapsed_since_release >= COOLDOWN_AFTER_RELEASE

    @staticmethod
    def should_accept_release(hold_duration: float) -> bool:
        """Mirrors the debounce check in the CGEventTap callback."""
        return hold_duration >= MIN_HOLD_DURATION

    def test_short_hold_rejected(self):
        """A tap shorter than MIN_HOLD_DURATION is ignored."""
        assert not self.should_accept_release(0.05)

    def test_normal_hold_accepted(self):
        """A hold of 0.3s (normal speech) is accepted."""
        assert self.should_accept_release(0.3)

    def test_long_hold_accepted(self):
        """A long hold (10s dictation) is accepted."""
        assert self.should_accept_release(10.0)

    def test_rapid_repress_rejected(self):
        """Pressing again immediately after release is rejected by cooldown."""
        assert not self.should_accept_press(0.1)

    def test_normal_repress_accepted(self):
        """Pressing again after a reasonable gap is accepted."""
        assert self.should_accept_press(1.0)

    def test_boundary_hold_duration(self):
        """Exactly at the threshold should be accepted."""
        assert self.should_accept_release(MIN_HOLD_DURATION)

    def test_boundary_cooldown(self):
        """Exactly at cooldown threshold should be accepted."""
        assert self.should_accept_press(COOLDOWN_AFTER_RELEASE)
