"""Tests for transition module — HFP-flip masking logic, no hardware/osascript."""

import threading
import time

import pytest

import transition
from transition import (
    TransitionManager,
    device_needs_masking,
    MODE_HOLD,
    MODE_FADE,
    MODE_PAUSE,
    MODE_OFF,
    DEFAULT_TRANSITION_MODE,
    DUCK_VOLUME,
)


@pytest.fixture
def fake_osa(monkeypatch):
    """Capture osascript calls; scriptable return value per call."""
    calls = []
    returns = {}

    def _fake(script, timeout=2.0):
        calls.append(script)
        for key, val in returns.items():
            if key in script:
                return val
        return ""

    monkeypatch.setattr(transition, "_osascript", _fake)
    monkeypatch.setattr(transition, "RESTORE_DELAY_S", 0.02)
    return {"calls": calls, "returns": returns}


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class TestDeviceNeedsMasking:
    def test_airpods_masked(self):
        assert device_needs_masking("Riaan's AirPods Pro 3")

    def test_beats_masked(self):
        assert device_needs_masking("Beats Studio Buds")

    def test_macbook_not_masked(self):
        assert not device_needs_masking("MacBook Air Microphone")

    def test_none_not_masked(self):
        assert not device_needs_masking(None)

    def test_usb_mic_not_masked(self):
        assert not device_needs_masking("Blue Yeti")


class TestDefaultMode:
    def test_default_is_hold(self):
        """hold is on by default (#269): the HFP flip jumps output volume on
        its own, so 'off' is not neutral — it just lets the spike through.
        Unlike fade (#266) it never ducks and only writes at open, so it
        cannot stomp a manual volume change mid-dictation."""
        assert DEFAULT_TRANSITION_MODE == MODE_HOLD

    def test_fade_is_not_the_default(self):
        assert DEFAULT_TRANSITION_MODE != MODE_FADE


class TestFadeMode:
    def test_pre_open_ducks_and_saves(self, fake_osa):
        fake_osa["returns"]["output volume"] = "65"
        tm = TransitionManager(mode=MODE_FADE)
        tm.pre_open("AirPods Pro 3")
        assert tm._saved_volume == 65
        assert any("set volume output volume" in c for c in fake_osa["calls"])

    def test_macbook_device_no_osascript(self, fake_osa):
        tm = TransitionManager(mode=MODE_FADE)
        tm.pre_open("MacBook Air Microphone")
        assert fake_osa["calls"] == []
        tm.post_close()
        assert fake_osa["calls"] == []

    def test_post_close_restores_after_delay(self, fake_osa):
        fake_osa["returns"]["output volume"] = "65"
        tm = TransitionManager(mode=MODE_FADE)
        tm.pre_open("AirPods Pro 3")
        tm.pre_close()
        tm.post_close()
        assert wait_for(
            lambda: any(c.strip() == "set volume output volume 65" for c in fake_osa["calls"])
        ), f"restore never fired: {fake_osa['calls']}"
        assert tm._carried is None

    def test_rapid_repress_preserves_original_volume(self, fake_osa, monkeypatch):
        """Re-press before the restore fires must NOT lose the real volume."""
        monkeypatch.setattr(transition, "RESTORE_DELAY_S", 5.0)  # never fires in test
        fake_osa["returns"]["output volume"] = "65"
        tm = TransitionManager(mode=MODE_FADE)
        tm.pre_open("AirPods Pro 3")
        tm.pre_close()
        tm.post_close()  # schedules restore in 5s (cancelled next)
        fake_osa["returns"]["output volume"] = str(DUCK_VOLUME)  # volume IS ducked now
        tm.pre_open("AirPods Pro 3")  # rapid re-press
        assert tm._saved_volume == 65  # carried, not re-read as 8

    def test_bad_volume_output_survives(self, fake_osa):
        fake_osa["returns"]["output volume"] = "not-a-number"
        tm = TransitionManager(mode=MODE_FADE)
        tm.pre_open("AirPods Pro 3")  # must not raise
        assert tm._saved_volume is None
        tm.pre_close()
        tm.post_close()

    def test_carried_debt_settled_when_device_changes(self, fake_osa, monkeypatch):
        """Pending restore + next recording on MacBook mic → restore now."""
        monkeypatch.setattr(transition, "RESTORE_DELAY_S", 5.0)
        fake_osa["returns"]["output volume"] = "65"
        tm = TransitionManager(mode=MODE_FADE)
        tm.pre_open("AirPods Pro 3")
        tm.post_close()
        fake_osa["calls"].clear()
        tm.pre_open("MacBook Air Microphone")
        assert any("set volume output volume 65" in c for c in fake_osa["calls"])


class TestPauseMode:
    def test_pauses_and_resumes_only_playing_players(self, fake_osa):
        fake_osa["returns"]["player state"] = "Spotify,"
        tm = TransitionManager(mode=MODE_PAUSE)
        tm.pre_open("AirPods Pro 3")
        assert tm._paused_players == ["Spotify"]
        tm.post_close()
        assert wait_for(
            lambda: any('tell application "Spotify" to play' in c for c in fake_osa["calls"])
        )
        assert not any('tell application "Music" to play' in c for c in fake_osa["calls"])

    def test_nothing_playing_nothing_resumed(self, fake_osa):
        fake_osa["returns"]["player state"] = ""
        tm = TransitionManager(mode=MODE_PAUSE)
        tm.pre_open("AirPods Pro 3")
        tm.post_close()
        time.sleep(0.05)
        assert not any("to play" in c for c in fake_osa["calls"])

    def test_rapid_repress_carries_paused_players(self, fake_osa, monkeypatch):
        monkeypatch.setattr(transition, "RESTORE_DELAY_S", 5.0)
        fake_osa["returns"]["player state"] = "Spotify,"
        tm = TransitionManager(mode=MODE_PAUSE)
        tm.pre_open("AirPods Pro 3")
        tm.post_close()  # resume scheduled far out
        fake_osa["returns"]["player state"] = ""  # nothing playing now (we paused it)
        tm.pre_open("AirPods Pro 3")  # rapid re-press cancels resume
        assert tm._paused_players == ["Spotify"]  # carried forward


class TestOffMode:
    def test_no_osascript_at_all(self, fake_osa):
        tm = TransitionManager(mode=MODE_OFF)
        tm.pre_open("AirPods Pro 3")
        tm.post_open()
        tm.pre_close()
        tm.post_close()
        assert fake_osa["calls"] == []


class TestRecorderHookSafety:
    def test_raising_hook_is_swallowed(self):
        from recorder import Recorder

        class BadHooks:
            def pre_open(self, name):
                raise RuntimeError("boom")

        r = Recorder(hooks=BadHooks())
        r._hook("pre_open", "AirPods")  # must not raise

    def test_missing_hook_is_ignored(self):
        from recorder import Recorder

        r = Recorder(hooks=object())
        r._hook("pre_open", "AirPods")  # must not raise

    def test_no_hooks_object(self):
        from recorder import Recorder

        r = Recorder()
        r._hook("post_close")  # must not raise


class TestHoldMode:
    """#269 — hold the A2DP volume across the HFP flip instead of ducking."""

    def test_writes_saved_volume_back_after_open(self, fake_osa, monkeypatch):
        # A2DP reads 38; the flip pushes HFP to 50, so a correction is owed.
        reads = iter(["38", "50"])

        def _fake(script, timeout=2.0):
            fake_osa["calls"].append(script)
            if "output volume of" in script:
                return next(reads)
            return ""

        monkeypatch.setattr(transition, "_osascript", _fake)
        tm = TransitionManager(mode=MODE_HOLD)
        tm.pre_open("AirPods Pro 3")
        tm.post_open()
        assert wait_for(
            lambda: any("set volume output volume 38" in c for c in fake_osa["calls"])
        ), fake_osa["calls"]

    def test_no_write_when_hfp_already_matches(self, fake_osa, monkeypatch):
        # Both reads return 38 — HFP already remembers it, so nothing to do.
        def _fake(script, timeout=2.0):
            fake_osa["calls"].append(script)
            return "38" if "output volume of" in script else ""

        monkeypatch.setattr(transition, "_osascript", _fake)
        tm = TransitionManager(mode=MODE_HOLD)
        tm.pre_open("AirPods Pro 3")
        tm.post_open()
        time.sleep(0.1)
        assert not any("set volume output volume" in c for c in fake_osa["calls"])

    def test_non_bluetooth_device_makes_no_volume_calls(self, fake_osa):
        tm = TransitionManager(mode=MODE_HOLD)
        tm.pre_open("MacBook Air Microphone")
        tm.post_open()
        time.sleep(0.1)
        assert fake_osa["calls"] == []

    def test_never_writes_on_close(self, fake_osa, monkeypatch):
        """A manual volume change mid-dictation must survive (#266)."""
        def _fake(script, timeout=2.0):
            fake_osa["calls"].append(script)
            return "38" if "output volume of" in script else ""

        monkeypatch.setattr(transition, "_osascript", _fake)
        tm = TransitionManager(mode=MODE_HOLD)
        tm.pre_open("AirPods Pro 3")
        tm.post_open()
        time.sleep(0.1)
        fake_osa["calls"].clear()
        tm.pre_close()
        tm.post_close()
        time.sleep(0.1)
        assert fake_osa["calls"] == []

    def test_is_the_default_mode(self):
        assert DEFAULT_TRANSITION_MODE == MODE_HOLD

    def test_pre_open_does_not_block_the_hotkey_path(self, fake_osa, monkeypatch):
        """pre_open sits on the hotkey press; a blocking osascript there
        delays the stream open and clips the first word (#269)."""
        def _slow(script, timeout=2.0):
            fake_osa["calls"].append(script)
            time.sleep(0.15)  # a real osascript round trip is ~124ms
            return "38"

        monkeypatch.setattr(transition, "_osascript", _slow)
        tm = TransitionManager(mode=MODE_HOLD)
        t = time.perf_counter()
        tm.pre_open("AirPods Pro 3")
        elapsed = time.perf_counter() - t
        assert elapsed < 0.05, f"pre_open blocked for {elapsed*1000:.0f}ms"

    def test_skips_correction_when_read_never_lands(self, fake_osa, monkeypatch):
        """No pre-flip level = no target; leave the user's volume alone."""
        monkeypatch.setattr(transition, "_get_output_volume", lambda: None)
        tm = TransitionManager(mode=MODE_HOLD)
        tm.pre_open("AirPods Pro 3")
        tm.post_open()
        time.sleep(0.15)
        assert not any("set volume output volume" in c for c in fake_osa["calls"])
