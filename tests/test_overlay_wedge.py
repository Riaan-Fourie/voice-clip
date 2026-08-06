"""Regression tests for issue #323 — a wedged overlay window must rebuild itself.

On 2026-08-06 the mascot had been invisible for six days while every log line
looked perfect. The overlay NSPanel is created once and never rebuilt, and after
enough uptime + Space churn macOS left it permanently unable to join any Space.
`show()` kept doing everything right — correct screen (#275), correct level
(#284), alpha 1.0 — onto a window the WindowServer would no longer display.

The rescue: after showing, ask the WindowServer (not AppKit) whether the panel
really landed on screen, and rebuild it if not. These tests lock in both the
rescue and the guards that stop it firing when it shouldn't.

Headless by design — no real windows, so this runs anywhere.
"""

import overlay


def _overlay(visible=True, onscreen=False, rebuilt=False):
    """A RecordingOverlay with every AppKit touchpoint stubbed out."""
    o = overlay.RecordingOverlay()
    o._window = object()          # non-None sentinel; never actually called
    o._view = object()
    o._visible = visible
    o._rebuilt_this_show = rebuilt
    o.calls = []
    o._is_onscreen = lambda: onscreen
    o._rebuild_window = lambda: o.calls.append("rebuild")
    o._present = lambda: o.calls.append("present")
    # the rebuild path resets the view's animation state
    o._rebuild_window = lambda: (o.calls.append("rebuild"),
                                 setattr(o, "_view", type("V", (), {})()))[0]
    return o


class TestWedgeRescue:
    def test_wedged_window_is_rebuilt_and_represented(self):
        o = _overlay(onscreen=False)
        o._verify_onscreen()
        assert o.calls == ["rebuild", "present"]
        assert o._rebuilt_this_show is True

    def test_healthy_window_is_left_alone(self):
        """Negative AC: no rebuild on a healthy window — that would thrash the
        WindowServer and restart the mascot animation on every recording."""
        o = _overlay(onscreen=True)
        o._verify_onscreen()
        assert o.calls == []
        assert o._rebuilt_this_show is False

    def test_at_most_one_rebuild_per_recording(self):
        """A wedge that survives the rebuild must not spin forever."""
        o = _overlay(onscreen=False)
        o._verify_onscreen()
        o._verify_onscreen()
        o._verify_onscreen()
        assert o.calls.count("rebuild") == 1

    def test_no_rescue_after_recording_ended(self):
        """hide() clears _visible; a late probe must not resurrect the overlay."""
        o = _overlay(visible=False, onscreen=False)
        o._verify_onscreen()
        assert o.calls == []

    def test_no_rescue_before_window_exists(self):
        o = _overlay(onscreen=False)
        o._window = None
        o._verify_onscreen()
        assert o.calls == []


class TestOnscreenProbe:
    def test_probe_failure_assumes_healthy(self):
        """A broken diagnostic must never take the overlay down with it."""
        o = overlay.RecordingOverlay()

        class Exploding:
            def windowNumber(self):
                raise RuntimeError("boom")

        o._window = Exploding()
        assert o._is_onscreen() is True

    def test_no_window_is_not_onscreen(self):
        o = overlay.RecordingOverlay()
        o._window = None
        assert o._is_onscreen() is False


class TestMascotCache:
    def test_frames_are_cached_so_rebuild_costs_no_disk_io(self):
        overlay._MASCOT_CACHE = None
        first = overlay._load_mascot()
        if first is None:
            return  # assets unavailable in this environment; nothing to assert
        assert overlay._load_mascot() is first


class TestProbeDelay:
    def test_delay_lands_inside_a_normal_recording(self):
        """Long enough for compositing to settle, short enough that the rescue
        still happens while the user is holding the key."""
        assert 0.1 <= overlay._ONSCREEN_CHECK_DELAY <= 1.0
