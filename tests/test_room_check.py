"""Auto mic (Jarvis #767): the room check that picks AirPods or the MacBook mic
for the next dictation. No hardware: audio is synthesised, streams are faked."""
from unittest import mock

import numpy as np
import pytest

import recorder
import room_check
from recorder import MIC_AIRPODS, MIC_AUTO, MIC_MACBOOK, SAMPLE_RATE
from room_check import NOISY_BELOW_DB, QUIET_ABOVE_DB, next_choice, voice_to_noise_db

RNG = np.random.default_rng(767)


def _clip(seconds, noise_rms, voice_rms):
    """Room noise throughout, with a voice in alternating 0.5 s bursts, so the
    clip has gaps between 'words' like real dictation."""
    n = int(seconds * SAMPLE_RATE)
    audio = RNG.normal(0, noise_rms, n)
    t = np.arange(n) / SAMPLE_RATE
    burst = (np.floor(t / 0.5) % 2) == 0
    audio[burst] += voice_rms * np.sqrt(2) * np.sin(2 * np.pi * 220 * t[burst])
    return np.clip(audio, -32768, 32767).astype(np.int16)


class TestScore:
    def test_too_short_is_unscored(self):
        assert voice_to_noise_db(_clip(0.8, 200, 2000)) is None

    def test_quiet_room_scores_high(self):
        assert voice_to_noise_db(_clip(4, 20, 1000)) > QUIET_ABOVE_DB

    def test_noisy_room_scores_low(self):
        # 2026-10-19: Mac mic noise ~190, voice ~310 -> measured 4.1 dB.
        assert voice_to_noise_db(_clip(4, 190, 150)) < NOISY_BELOW_DB

    def test_digital_silence_does_not_divide_by_zero(self):
        assert voice_to_noise_db(np.zeros(2 * SAMPLE_RATE, dtype=np.int16)) == pytest.approx(0.0)


class TestNextChoice:
    def test_noisy_goes_to_airpods(self):
        assert next_choice(MIC_MACBOOK, NOISY_BELOW_DB - 0.1) == MIC_AIRPODS

    def test_quiet_goes_to_macbook(self):
        assert next_choice(MIC_AIRPODS, QUIET_ABOVE_DB + 0.1) == MIC_MACBOOK

    @pytest.mark.parametrize("current", [MIC_AIRPODS, MIC_MACBOOK])
    def test_between_thresholds_keeps_the_mic(self, current):
        middle = (NOISY_BELOW_DB + QUIET_ABOVE_DB) / 2
        assert next_choice(current, middle) == current

    def test_no_score_keeps_the_mic(self):
        assert next_choice(MIC_AIRPODS, None) == MIC_AIRPODS


class FakeStream:
    def __init__(self, callback=None):
        self.callback = callback
        self.closed = False
    def start(self): pass
    def stop(self): pass
    def close(self): self.closed = True


DEVICES = [
    {"name": "AirPods Pro 3", "max_input_channels": 1},
    {"name": "MacBook Air Microphone", "max_input_channels": 1},
]


@pytest.fixture
def rec(monkeypatch):
    monkeypatch.setattr(recorder.sd, "query_devices", lambda *a, **k: DEVICES)
    monkeypatch.setattr(recorder, "_begin_no_nap", lambda: None)
    probes = []

    def fake_input_stream(**kw):
        s = FakeStream(kw["callback"])
        probes.append((kw["device"], s))
        return s

    monkeypatch.setattr(recorder.sd, "InputStream", fake_input_stream)
    r = recorder.Recorder(mic_preference=MIC_AUTO)
    r._open_stream = lambda device: FakeStream()
    r.probes = probes
    return r


def _feed(callback, audio):
    for i in range(0, len(audio), 1024):
        callback(audio[i:i + 1024].reshape(-1, 1), None, None, None)


class TestRecorderAuto:
    def test_auto_uses_its_current_choice(self, rec):
        rec.auto_choice = MIC_AIRPODS
        rec.start()
        assert rec.last_device_name == "AirPods Pro 3"
        rec.stop()

    def test_airpods_recording_probes_the_macbook_mic(self, rec):
        rec.auto_choice = MIC_AIRPODS
        rec.start()
        assert [d for d, _ in rec.probes] == [1]  # the MacBook mic, never the AirPods
        audio = _clip(2, 50, 1000)
        _feed(rec.probes[0][1].callback, audio)
        rec.stop()
        assert rec.probes[0][1].closed
        np.testing.assert_array_equal(rec.take_probe_audio(), audio)

    def test_macbook_recording_is_its_own_probe(self, rec):
        rec.auto_choice = MIC_MACBOOK
        rec.start()
        assert rec.probes == []  # no second stream
        audio = _clip(2, 50, 1000)
        _feed(rec._audio_callback, audio)
        rec.stop()
        np.testing.assert_array_equal(rec.take_probe_audio(), audio)

    def test_probe_is_cleared_on_read(self, rec):
        rec.auto_choice = MIC_MACBOOK
        rec.start()
        _feed(rec._audio_callback, _clip(2, 50, 1000))
        rec.stop()
        assert rec.take_probe_audio() is not None
        assert rec.take_probe_audio() is None

    def test_probe_failure_never_breaks_recording(self, rec, monkeypatch):
        def boom(**kw):
            raise OSError("PaErrorCode -9986")
        monkeypatch.setattr(recorder.sd, "InputStream", boom)
        rec.auto_choice = MIC_AIRPODS
        rec.start()
        assert rec.is_recording
        assert rec.stop()[:4] == b"RIFF"
        assert rec.take_probe_audio() is None

    def test_fixed_preference_opens_no_probe(self, rec):
        rec.mic_preference = MIC_AIRPODS
        rec.start()
        rec.stop()
        assert rec.probes == []
        assert rec.take_probe_audio() is None


@pytest.fixture
def app(monkeypatch):
    import main

    rec = mock.Mock()
    rec.mic_preference = MIC_AUTO
    rec.auto_choice = MIC_MACBOOK
    saved = {}
    monkeypatch.setattr(main, "load_settings", lambda: {})
    monkeypatch.setattr(main, "save_settings", lambda s: saved.update(s))
    monkeypatch.setattr(main, "_log", lambda *a, **k: None)
    banners = []
    monkeypatch.setattr(main, "_show_banner", lambda sub, msg: banners.append(sub))
    with mock.patch.object(main, "Recorder", return_value=rec), \
         mock.patch.object(main, "Transcriber", return_value=mock.Mock(get_failed_count=lambda: 0)), \
         mock.patch.object(main, "HotkeyListener", return_value=mock.Mock()), \
         mock.patch.object(main, "RecordingOverlay", return_value=mock.Mock()), \
         mock.patch.object(main.threading, "Thread", return_value=mock.Mock()):
        a = main.VoiceClipApp()
    a.recorder = rec
    a._notify = mock.Mock()
    a.saved = saved
    a.banners = banners
    return a


def main_mod():
    import main
    return main


class TestAppRoomCheck:
    def test_noisy_room_switches_next_dictation_to_airpods(self, app):
        app.recorder.take_probe_audio.return_value = _clip(4, 190, 150)
        app._room_check()
        assert app.recorder.auto_choice == MIC_AIRPODS
        assert app.saved["auto_mic_choice"] == MIC_AIRPODS
        assert app.banners == ["Mic: AirPods"]

    def test_quiet_room_switches_back_to_macbook(self, app):
        app.recorder.auto_choice = MIC_AIRPODS
        app.recorder.take_probe_audio.return_value = _clip(4, 20, 1000)
        app._room_check()
        assert app.recorder.auto_choice == MIC_MACBOOK
        assert app.banners == ["Mic: MacBook"]

    def test_unchanged_choice_is_silent(self, app):
        app.recorder.take_probe_audio.return_value = _clip(4, 20, 1000)
        app._room_check()
        assert app.recorder.auto_choice == MIC_MACBOOK
        assert app.banners == []
        assert app.saved == {}

    def test_fixed_preference_never_checks(self, app):
        app.recorder.mic_preference = MIC_AIRPODS
        app._room_check()
        app.recorder.take_probe_audio.assert_not_called()

    def test_check_runs_only_after_speech(self, app):
        app.transcriber.transcribe.return_value = (None, None)  # no speech
        app.recorder.take_probe_audio.return_value = _clip(4, 190, 150)
        with mock.patch.object(app, "_room_check") as check:
            app._do_transcribe(b"x")
        check.assert_not_called()
        app.recorder.take_probe_audio.assert_called()  # stale clip discarded

    def test_punctuation_only_transcript_skips_the_check(self, app):
        app.transcriber.transcribe.return_value = ("!", None)
        with mock.patch.object(main_mod(), "_copy_to_clipboard"), \
             mock.patch.object(main_mod(), "_paste_from_clipboard"), \
             mock.patch.object(app, "_room_check") as check:
            app._do_transcribe(b"x")
        check.assert_not_called()

    def test_words_run_the_check(self, app):
        app.transcriber.transcribe.return_value = ("Send it to Dewald.", None)
        with mock.patch.object(main_mod(), "_copy_to_clipboard"), \
             mock.patch.object(main_mod(), "_paste_from_clipboard"), \
             mock.patch.object(app, "_room_check") as check:
            app._do_transcribe(b"x")
        check.assert_called_once()

    def test_restart_restores_the_last_choice(self, monkeypatch):
        import main
        rec = mock.Mock()
        monkeypatch.setattr(main, "load_settings", lambda: {"mic_preference": "auto", "auto_mic_choice": "airpods"})
        with mock.patch.object(main, "Recorder", return_value=rec), \
             mock.patch.object(main, "Transcriber", return_value=mock.Mock(get_failed_count=lambda: 0)), \
             mock.patch.object(main, "HotkeyListener", return_value=mock.Mock()), \
             mock.patch.object(main, "RecordingOverlay", return_value=mock.Mock()), \
             mock.patch.object(main.threading, "Thread", return_value=mock.Mock()):
            main.VoiceClipApp()
        assert rec.auto_choice == MIC_AIRPODS


def test_banner_quotes_survive_osascript(monkeypatch):
    import main
    calls = []
    monkeypatch.setattr(main.subprocess, "Popen", lambda args, **kw: calls.append(args))
    main._show_banner('Mic: "AirPods"', 'a \\ b')
    script = calls[0][2]
    assert script == 'display notification "a \\\\ b" with title "VoiceClip" subtitle "Mic: \\"AirPods\\""'
