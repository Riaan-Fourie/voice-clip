"""Tests for transition module — HFP-flip masking logic, no hardware/osascript."""

import threading
import time

import pytest

import transition
from transition import (
    TransitionManager,
    device_needs_masking,
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
    def test_default_is_fade(self):
        assert DEFAULT_TRANSITION_MODE == MODE_FADE


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
