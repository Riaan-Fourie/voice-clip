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


import threading
import time


def _make_app(**state):
    """Build a bare VoiceClipApp instance (no __init__) with the given attrs."""
    import main

    app = main.VoiceClipApp.__new__(main.VoiceClipApp)
    # Lock mode (#273) made the press/release handlers take _rec_lock; bypassing
    # __init__ leaves it unset, so supply it here or those paths raise.
    app._rec_lock = threading.RLock()
    for k, v in state.items():
        setattr(app, k, v)
    return app


def _live_thread():
    """A thread that stays alive (blocked on an Event) for the test's duration."""
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    return t, stop


def _dead_thread():
    """A thread that has already finished (is_alive() is False)."""
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    assert not t.is_alive()
    return t


class TestWedgedTranscriberRecovery:
    """Issue #187 / jarvis-system #6 — a transcription wedged inside the native
    mlx_whisper/Metal call must trigger a process re-exec, not just a gate reset."""

    def test_busy_when_not_transcribing(self):
        import main

        app = _make_app(_transcribing=False, _transcribe_started_at=0.0,
                        _transcribe_thread=None)
        assert main.VoiceClipApp._stuck_recovery_action(app) == "busy"

    def test_busy_within_timeout(self):
        """A live worker only 1s in is legitimately working — leave it alone."""
        import main

        t, stop = _live_thread()
        try:
            app = _make_app(_transcribing=True,
                            _transcribe_started_at=time.monotonic() - 1.0,
                            _transcribe_thread=t)
            assert main.VoiceClipApp._stuck_recovery_action(app) == "busy"
        finally:
            stop.set()

    def test_reexec_when_wedged_thread_still_alive(self):
        """Past the timeout AND the worker is still alive = native wedge → re-exec."""
        import main

        t, stop = _live_thread()
        try:
            app = _make_app(_transcribing=True,
                            _transcribe_started_at=time.monotonic() - 999,
                            _transcribe_thread=t)
            assert main.VoiceClipApp._stuck_recovery_action(app) == "reexec"
        finally:
            stop.set()

    def test_reset_when_gate_leaked_thread_dead(self):
        """Past the timeout but the worker is gone = leaked gate → cheap reset."""
        import main

        app = _make_app(_transcribing=True,
                        _transcribe_started_at=time.monotonic() - 999,
                        _transcribe_thread=_dead_thread())
        assert main.VoiceClipApp._stuck_recovery_action(app) == "reset"

    def test_press_reexecs_on_live_wedge(self):
        """`_on_hotkey_press` must re-exec (not start a new recording) when the
        in-flight transcription is wedged in native code."""
        import main

        t, stop = _live_thread()
        try:
            calls = []

            def fake_reexec():
                calls.append(True)
                raise SystemExit  # os.execv never returns; mimic that here

            app = _make_app(_transcribing=True,
                            _transcribe_started_at=time.monotonic() - 999,
                            _transcribe_thread=t, _key_pressed=False)
            app._reexec = fake_reexec

            with pytest.raises(SystemExit):
                app._on_hotkey_press()
            assert calls == [True]
        finally:
            stop.set()

    def test_watchdog_reexecs_live_wedge(self, monkeypatch):
        """The background watchdog must re-exec a live wedge with no user keypress."""
        import main

        monkeypatch.setattr(main, "TRANSCRIBE_WATCHDOG_INTERVAL", 0.02)
        t, stop = _live_thread()
        try:
            fired = threading.Event()

            def fake_reexec():
                fired.set()
                raise SystemExit  # breaks the while-True loop like execv would

            app = _make_app(_transcribing=True,
                            _transcribe_started_at=time.monotonic() - 999,
                            _transcribe_thread=t)
            app._reexec = fake_reexec

            runner = threading.Thread(
                target=lambda: _swallow(app._transcribe_watchdog), daemon=True
            )
            runner.start()
            assert fired.wait(timeout=2.0), "watchdog never re-execd a live wedge"
        finally:
            stop.set()

    def test_watchdog_resets_leaked_gate_without_reexec(self, monkeypatch):
        """A leaked gate (dead worker) must be reset, NOT re-exec'd."""
        import main

        monkeypatch.setattr(main, "TRANSCRIBE_WATCHDOG_INTERVAL", 0.02)

        def boom():
            raise AssertionError("must not re-exec a leaked gate")

        app = _make_app(_transcribing=True,
                        _transcribe_started_at=time.monotonic() - 999,
                        _transcribe_thread=_dead_thread())
        app._reexec = boom

        runner = threading.Thread(
            target=lambda: _swallow(app._transcribe_watchdog), daemon=True
        )
        runner.start()
        # Give the watchdog a few iterations to act.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and app._transcribing:
            time.sleep(0.02)
        assert app._transcribing is False


def _swallow(fn):
    """Run a watchdog loop, swallowing the SystemExit a fake _reexec raises."""
    try:
        fn()
    except SystemExit:
        pass
