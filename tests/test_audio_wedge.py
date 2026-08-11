"""Regression tests for issue #333 — a deadlocked CoreAudio call must not kill dictation.

On 2026-08-11 VoiceClip was "down" for 31 minutes while the process was alive, the
menu-bar icon was present and the CGEventTap was happily logging every keypress.
`recorder.stop()` had deadlocked inside the CoreAudio HAL: PortAudio's
`startStopCallback` called `AudioUnitGetProperty` from the CoreAudio IO workloop
(holding the HAL mutex, wanting the AudioToolbox mutex) while `FinishStoppingStream`
on the Python thread held AudioToolbox and wanted HAL. A textbook ABBA deadlock —
1647/1647 stack samples frozen in it.

Two things turned one stuck recording into a dead app:

  1. `_stop_and_transcribe` holds `_rec_lock` across that blocking call, so every
     later press blocked forever *inside the CGEventTap callback*.
  2. All four recovery mechanisms were blind to it — the transcribe watchdog only
     watches `_transcribing`, the gate-reset escalation needs 3 consecutive resets
     (a hung stop yields exactly 1), the press-path backstop lives INSIDE the very
     lock that's held, and the external watchdog only asked whether the process
     existed. It did.

These tests lock in the fix: the audio call is now tracked, the watchdog re-execs on
it, and no hotkey handler can block on the lock forever.
"""

import threading
import time

import pytest


def _lock_is_free(lock) -> bool:
    """True if `lock` can be taken by an unrelated thread.

    The probe must acquire AND release on the same thread: an RLock is owner-tracked,
    so releasing it from anywhere else raises instead of answering the question.
    """
    got = []

    def probe():
        if lock.acquire(timeout=1):
            got.append(True)
            lock.release()

    t = threading.Thread(target=probe)
    t.start()
    t.join(timeout=5)
    return got == [True]


def _make_app(**state):
    """Build a bare VoiceClipApp instance (no __init__) with the given attrs."""
    import main

    app = main.VoiceClipApp.__new__(main.VoiceClipApp)
    app._rec_lock = threading.RLock()
    app._audio_call_name = None
    app._audio_call_started_at = 0.0
    app._lock_timeout_logged = False
    for k, v in state.items():
        setattr(app, k, v)
    return app


class TestAudioCallTracking:
    """The watchdog can only recover what the app admits it is doing."""

    def test_no_call_in_flight_reads_zero(self):
        import main

        app = _make_app()
        assert main.VoiceClipApp._audio_wedge_seconds(app) == 0.0

    def test_in_flight_call_is_visible_while_it_runs(self):
        """The whole fix depends on this being observable *during* the call."""
        import main

        app = _make_app()
        seen = {}
        with main.VoiceClipApp._audio_call(app, "recorder.stop"):
            seen["name"] = app._audio_call_name
            seen["elapsed"] = main.VoiceClipApp._audio_wedge_seconds(app)
        assert seen["name"] == "recorder.stop"
        assert seen["elapsed"] >= 0.0
        # …and cleared on the way out, so a healthy call never looks wedged.
        assert app._audio_call_started_at == 0.0
        assert main.VoiceClipApp._audio_wedge_seconds(app) == 0.0

    def test_guard_clears_even_when_the_call_raises(self):
        """A raising recorder.stop() must not leave a permanent phantom wedge —
        that would re-exec the app on a loop."""
        import main

        app = _make_app()
        with pytest.raises(RuntimeError):
            with main.VoiceClipApp._audio_call(app, "recorder.start"):
                raise RuntimeError("mic exploded")
        assert app._audio_call_started_at == 0.0
        assert main.VoiceClipApp._audio_wedge_seconds(app) == 0.0

    def test_watchdog_reads_state_without_taking_rec_lock(self):
        """In a real wedge `_rec_lock` is held by the deadlocked thread. If the
        watchdog needed it, the watchdog would deadlock too — the exact trap the
        press-path backstop fell into."""
        import main

        app = _make_app(_audio_call_started_at=time.monotonic() - 999,
                        _audio_call_name="recorder.stop")
        app._rec_lock.acquire()  # simulate the deadlocked thread holding it
        try:
            done = []

            def probe():
                done.append(main.VoiceClipApp._audio_wedge_seconds(app))

            t = threading.Thread(target=probe)
            t.start()
            t.join(timeout=5)
            assert not t.is_alive(), "watchdog probe blocked on _rec_lock"
            assert done and done[0] > 900
        finally:
            app._rec_lock.release()


class TestWatchdogRecovery:
    """A wedged audio call is unrecoverable in-process: the CoreAudio HAL mutex is
    process-global. Re-exec is the only fix, so the watchdog must reach for it."""

    def test_timeout_constant_is_sane(self):
        import main

        # A real start/stop is milliseconds; must be well above that and well below
        # the point where the user has given up and restarted by hand.
        assert 2.0 <= main.AUDIO_CALL_STUCK_TIMEOUT <= 60.0

    def _run_one_watchdog_pass(self, app, monkeypatch):
        """Drive exactly one iteration of the real watchdog loop."""
        import main

        monkeypatch.setattr(main.time, "sleep", lambda _s: None)
        calls = []
        app._reexec = lambda: (calls.append(True), (_ for _ in ()).throw(SystemExit))[0]
        with pytest.raises(SystemExit):
            main.VoiceClipApp._transcribe_watchdog(app)
        return calls

    def test_watchdog_reexecs_on_wedged_audio_call(self, monkeypatch):
        import main

        app = _make_app(
            _audio_call_started_at=time.monotonic() - (main.AUDIO_CALL_STUCK_TIMEOUT + 5),
            _audio_call_name="recorder.stop",
            _transcribing=False, _transcribe_started_at=0.0, _transcribe_thread=None,
        )
        assert self._run_one_watchdog_pass(app, monkeypatch) == [True]

    def test_watchdog_leaves_a_healthy_call_alone(self, monkeypatch):
        """A call that started 1s ago is working, not wedged. Re-execing here would
        destroy a recording mid-flight."""
        import main

        app = _make_app(
            _audio_call_started_at=time.monotonic() - 1.0,
            _audio_call_name="recorder.stop",
            _transcribing=False, _transcribe_started_at=0.0, _transcribe_thread=None,
        )
        monkeypatch.setattr(main.time, "sleep", lambda _s: None)
        app._reexec = lambda: pytest.fail("re-exec on a healthy audio call")

        # Run the loop briefly in a thread; it must simply keep looping.
        t = threading.Thread(target=main.VoiceClipApp._transcribe_watchdog,
                             args=(app,), daemon=True)
        t.start()
        time.sleep(0.2)
        assert t.is_alive()

    def test_audio_wedge_beats_transcription_wedge(self, monkeypatch):
        """Both wedged at once must resolve as the AUDIO wedge: it is the one that
        also holds `_rec_lock`, and the log line has to name the real culprit."""
        import main

        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()

        app = _make_app(
            _audio_call_started_at=time.monotonic() - 999,
            _audio_call_name="recorder.stop",
            # A leaked gate too — the cheap "reset" path would otherwise win.
            _transcribing=True, _transcribe_started_at=time.monotonic() - 999,
            _transcribe_thread=dead, _consecutive_resets=0,
        )
        assert self._run_one_watchdog_pass(app, monkeypatch) == [True]
        # It must NOT have been written off as a mere gate leak.
        assert app._transcribing is True, "audio wedge misdiagnosed as a gate leak"


class TestHotkeyNeverBlocksForever:
    """The CGEventTap callback must always return promptly. macOS disables a tap that
    doesn't — which in #333 would have cost the hotkey itself on top of the wedge."""

    @pytest.mark.parametrize("handler", ["_on_hotkey_press", "_on_hotkey_release",
                                         "_on_hotkey_cancel", "_on_hotkey_toggle"])
    def test_handler_gives_up_when_lock_is_held(self, handler, monkeypatch):
        import main

        monkeypatch.setattr(main, "REC_LOCK_ACQUIRE_TIMEOUT", 0.1)
        app = _make_app(_recording=False, _mode=None, _key_pressed=False,
                        _transcribing=False, _transcribe_started_at=0.0,
                        _transcribe_thread=None,
                        _audio_call_started_at=time.monotonic() - 999,
                        _audio_call_name="recorder.stop")
        app._start_recording = lambda mode: pytest.fail(
            "started a recording while the audio stack was wedged")

        holder_release = threading.Event()
        holding = threading.Event()

        def hold_lock():
            with app._rec_lock:
                holding.set()
                holder_release.wait(timeout=10)

        t = threading.Thread(target=hold_lock, daemon=True)
        t.start()
        assert holding.wait(timeout=5)
        try:
            done = threading.Event()

            def call_handler():
                getattr(main.VoiceClipApp, handler)(app)
                done.set()

            h = threading.Thread(target=call_handler, daemon=True)
            h.start()
            assert done.wait(timeout=5), f"{handler} blocked on _rec_lock"
        finally:
            holder_release.set()

    def test_lock_is_released_after_a_normal_gesture(self):
        """The timeout rewrite hand-rolls acquire/release — a leaked release would
        wedge the app exactly like the bug it's meant to prevent."""
        import main

        app = _make_app(_recording=False, _mode=None, _key_pressed=False,
                        _transcribing=False, _transcribe_started_at=0.0,
                        _transcribe_thread=None)
        started = []
        app._start_recording = lambda mode: started.append(mode)

        main.VoiceClipApp._on_hotkey_press(app)
        assert started == ["hold"]
        assert _lock_is_free(app._rec_lock), "_rec_lock was not released after the gesture"

    def test_lock_is_released_even_when_the_handler_raises(self):
        import main

        app = _make_app(_recording=False, _mode=None, _key_pressed=False,
                        _transcribing=False, _transcribe_started_at=0.0,
                        _transcribe_thread=None)

        def boom(mode):
            raise RuntimeError("overlay blew up")

        app._start_recording = boom
        with pytest.raises(RuntimeError):
            main.VoiceClipApp._on_hotkey_press(app)

        assert _lock_is_free(app._rec_lock), "_rec_lock leaked when the handler raised"
