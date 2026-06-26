"""Regression tests for issue #170 — a bad transcription must not wedge dictation.

Two independent failures combined to silently kill the app on 2026-06-26:
  1. A format-3 (IEEE-float) WAV made `_wav_bytes_to_float32` raise
     `wave.Error: unknown format: 3`.
  2. `_do_transcribe` ran on a daemon thread and only cleared `_transcribing` as
     its last statement (not in `finally`), so that raise left the gate stuck True
     forever and every future hotkey press was ignored.

These tests lock in both fixes so the regression can't return.
"""

import io
import wave

import numpy as np
import pytest
import soundfile as sf

from transcriber import _wav_bytes_to_float32


def _float_wav(seconds=0.5, rate=16000):
    """A format-3 (IEEE-float) WAV — exactly what stdlib `wave` rejects."""
    sig = (np.sin(np.linspace(0, 60, int(seconds * rate))) * 0.5).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, sig, rate, subtype="FLOAT", format="WAV")
    return buf.getvalue(), sig


def _pcm16_wav(seconds=0.5, rate=16000):
    """A 16-bit PCM WAV — what the Recorder actually emits (fast path)."""
    sig = (np.sin(np.linspace(0, 60, int(seconds * rate))) * 0.5).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, (sig * 32767).astype(np.int16), rate, subtype="PCM_16", format="WAV")
    return buf.getvalue()


class TestWavDecodeFallback:
    def test_stdlib_wave_really_rejects_float_wav(self):
        """Guards the premise: stdlib wave raises the exact error we saw in prod."""
        wav, _ = _float_wav()
        with pytest.raises(Exception) as exc:
            with wave.open(io.BytesIO(wav), "rb") as wf:
                wf.readframes(wf.getnframes())
        assert "unknown format: 3" in str(exc.value)

    def test_float_wav_decodes_instead_of_raising(self):
        """The fix: a format-3 WAV must decode, not crash."""
        wav, sig = _float_wav()
        out = _wav_bytes_to_float32(wav)
        assert out.dtype == np.float32
        assert out.shape[0] == sig.shape[0]
        assert -1.0 <= float(out.min()) and float(out.max()) <= 1.0

    def test_pcm_still_uses_fast_path(self):
        """PCM input (the common case) must keep decoding correctly."""
        out = _wav_bytes_to_float32(_pcm16_wav())
        assert out.dtype == np.float32
        assert out.shape[0] == 8000


class TestTranscribeStuckTimeout:
    def test_timeout_constant_is_sane(self):
        """The backstop must be well above a real small.en transcription (<1s)."""
        import main

        assert 5.0 <= main.TRANSCRIBE_STUCK_TIMEOUT <= 120.0

    def test_safe_transcribe_clears_flag_on_crash(self):
        """If `_do_transcribe` raises, `_transcribing` must still be cleared —
        otherwise the app is dead-while-running (the core #170 wedge)."""
        import main

        class FakeApp:
            _transcribing = True
            title = ""

            def _do_transcribe(self, wav_bytes):
                raise RuntimeError("boom from a UI/notify call off the main thread")

        app = FakeApp()
        # Call the real wrapper bound to our fake — no rumps app instance needed.
        main.VoiceClipApp._safe_transcribe(app, b"\x00\x00")
        assert app._transcribing is False
