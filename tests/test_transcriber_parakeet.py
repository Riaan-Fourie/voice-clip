"""Transcriber on Parakeet (Jarvis #769): audio stays in memory, names are
corrected, and empty or failed clips go to the retry folder. The model itself
is faked, so this runs without a download or a GPU."""
import io
import wave
from unittest import mock

import numpy as np
import pytest

import transcriber


def _wav(seconds, value=1000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(np.full(int(seconds * 16000), value, dtype=np.int16).tobytes())
    return buf.getvalue()


@pytest.fixture
def t(monkeypatch, tmp_path):
    monkeypatch.setattr(transcriber, "FAILED_DIR", str(tmp_path / "failed"))
    monkeypatch.setattr(transcriber, "_load_proper_nouns", lambda: ["HashDirectors"])
    monkeypatch.setattr(transcriber, "load_replacements", lambda: [("to Wald", "Dewald")])
    tr = transcriber.Transcriber()
    tr._model = object()
    tr.heard = []
    def fake_run(model, audio):
        tr.heard.append(audio)
        return tr.reply
    monkeypatch.setattr(transcriber.Transcriber, "_run", staticmethod(fake_run))
    tr.reply = "send to Wald the hash directors link"
    return tr


def test_names_are_corrected(t):
    assert t.transcribe(_wav(2)) == ("send Dewald the HashDirectors link", None)


def test_audio_reaches_the_model_as_float32_in_memory(t):
    t.transcribe(_wav(2, value=16384))
    audio = t.heard[0]
    assert audio.dtype == np.float32 and len(audio) == 32000
    assert audio[0] == pytest.approx(0.5)


def test_empty_result_is_saved_for_retry(t):
    t.reply = ""
    assert t.transcribe(_wav(2)) == (None, "No speech detected")
    assert t.get_failed_count() == 1


def test_too_short_clip_skips_the_model(t):
    assert t.transcribe(_wav(0.05)) == (None, "No speech detected")
    assert t.heard == []


def test_model_that_failed_to_load_reports_an_error(t):
    t._model = None
    with mock.patch.object(transcriber.Transcriber, "_ensure_init"):
        text, err = t.transcribe(_wav(2))
    assert text is None and "not loaded" in err
    assert t.get_failed_count() == 1


def test_model_is_parakeet_v2():
    assert transcriber.MODEL == "mlx-community/parakeet-tdt-0.6b-v2"


def test_model_memory_is_pinned_before_the_model_loads(monkeypatch):
    calls = []
    monkeypatch.setattr(transcriber.mx, "set_wired_limit", lambda n: calls.append(("wire", n)))
    monkeypatch.setattr(transcriber.mx, "device_info", lambda: {"max_recommended_working_set_size": 12_000_000_000})
    fake = mock.Mock()
    fake.from_pretrained.side_effect = lambda m: calls.append(("load", m)) or object()
    monkeypatch.setitem(__import__("sys").modules, "parakeet_mlx", fake)
    monkeypatch.setattr(transcriber.Transcriber, "_run", staticmethod(lambda model, audio: ""))
    monkeypatch.setattr(transcriber, "load_replacements", lambda: [])
    monkeypatch.setattr(transcriber.nn, "quantize", lambda model, group_size, bits: calls.append(("quantize", bits)))
    tr = transcriber.Transcriber()
    tr._ensure_init()
    assert calls[0] == ("wire", transcriber.WIRED_LIMIT_BYTES)
    assert calls[1][0] == "load"
    assert calls[2] == ("quantize", 8)  # before the warm-up and before any dictation
    assert tr._model_loaded


def test_wired_limit_never_exceeds_half_the_system_cap(monkeypatch):
    got = []
    monkeypatch.setattr(transcriber.mx, "set_wired_limit", got.append)
    monkeypatch.setattr(transcriber.mx, "device_info", lambda: {"max_recommended_working_set_size": 2_000_000_000})
    transcriber._pin_model_memory()
    assert got == [1_000_000_000]


def test_wiring_failure_does_not_block_loading(monkeypatch):
    def boom(n):
        raise RuntimeError("not supported")
    monkeypatch.setattr(transcriber.mx, "set_wired_limit", boom)
    transcriber._pin_model_memory()  # must not raise


class TestKeepWarm:
    def test_runs_after_a_full_idle_interval(self, t, monkeypatch):
        t._last_used -= transcriber.KEEP_WARM_SECONDS + 1
        assert t._keep_warm_once() is True
        assert len(t.heard) == 1 and len(t.heard[0]) == 8000

    def test_skips_when_dictation_was_recent(self, t):
        t.transcribe(_wav(2))
        t.heard.clear()
        assert t._keep_warm_once() is False
        assert t.heard == []

    def test_never_waits_for_a_real_transcription(self, t):
        t._last_used -= transcriber.KEEP_WARM_SECONDS + 1
        with t._run_lock:  # a dictation is mid-transcription
            assert t._keep_warm_once() is False
        assert t.heard == []

    def test_a_failed_pass_never_raises(self, t, monkeypatch):
        t._last_used -= transcriber.KEEP_WARM_SECONDS + 1
        def boom(model, audio):
            raise RuntimeError("metal")
        monkeypatch.setattr(transcriber.Transcriber, "_run", staticmethod(boom))
        assert t._keep_warm_once() is False
        assert not t._run_lock.locked()

    def test_loading_the_model_starts_the_loop_once(self, monkeypatch):
        started = []
        monkeypatch.setattr(transcriber.threading, "Thread", lambda **kw: mock.Mock(start=lambda: started.append(kw["target"].__name__)))
        monkeypatch.setattr(transcriber, "load_replacements", lambda: [])
        tr = transcriber.Transcriber()
        tr._start_keep_warm(); tr._start_keep_warm()
        assert started == ["_keep_warm_loop"]
