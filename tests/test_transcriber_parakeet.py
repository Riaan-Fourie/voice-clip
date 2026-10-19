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
