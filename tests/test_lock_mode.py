"""Lock-mode (hands-free toggle) state machine + chord detection + watchdog
gate-reset escalation. Heavy deps (model, CGEventTap, overlay window) are mocked
so this runs fast and headless."""
import sys
import types
from unittest import mock

import pytest


@pytest.fixture
def app():
    """A VoiceClipApp with recorder/transcriber/hotkey/overlay stubbed out."""
    import main

    rec = mock.Mock()
    rec.last_device_name = "TestMic"
    rec.stop.return_value = b""          # short → no transcribe thread spawned
    rec.mic_preference = "airpods"

    with mock.patch.object(main, "Recorder", return_value=rec), \
         mock.patch.object(main, "Transcriber", return_value=mock.Mock(get_failed_count=lambda: 0)), \
         mock.patch.object(main, "HotkeyListener", return_value=mock.Mock()), \
         mock.patch.object(main, "RecordingOverlay", return_value=mock.Mock()), \
         mock.patch.object(main.threading, "Thread", return_value=mock.Mock()):
        a = main.VoiceClipApp()
    a.recorder = rec
    a._notify = mock.Mock()
    return a


def test_toggle_from_idle_starts_locked(app):
    app._on_hotkey_toggle()
    assert app._recording is True
    assert app._mode == "locked"
    app.recorder.start.assert_called_once()


def test_locked_ignores_key_release(app):
    app._on_hotkey_toggle()             # start locked
    app.recorder.stop.reset_mock()
    app._on_hotkey_release()            # a stray Right-Cmd release must NOT stop it
    assert app._recording is True
    app.recorder.stop.assert_not_called()


def test_second_toggle_stops(app):
    app._on_hotkey_toggle()             # start locked
    app._on_hotkey_toggle()             # stop
    assert app._recording is False
    assert app._mode is None
    app.recorder.stop.assert_called_once()


def test_hold_then_toggle_promotes_to_locked(app):
    app._on_hotkey_press()              # hold
    assert app._mode == "hold"
    app._on_hotkey_toggle()             # promote
    assert app._mode == "locked"
    app.recorder.start.assert_called_once()   # not restarted
    # now a Right-Cmd release is ignored (it was promoted)
    app.recorder.stop.reset_mock()
    app._on_hotkey_release()
    app.recorder.stop.assert_not_called()


def test_plain_hold_still_works(app):
    app._on_hotkey_press()
    assert app._mode == "hold" and app._recording
    app._on_hotkey_release()
    assert app._recording is False
    app.recorder.stop.assert_called_once()


def test_gate_reset_escalates_to_reexec(app):
    app._reexec = mock.Mock(side_effect=RuntimeError("execv"))
    # first two resets just count
    app._note_gate_reset()
    app._note_gate_reset()
    app._reexec.assert_not_called()
    # third consecutive reset crosses RESET_ESCALATE_THRESHOLD → re-exec
    with pytest.raises(RuntimeError):
        app._note_gate_reset()
    app._reexec.assert_called_once()


def test_successful_transcription_clears_reset_counter(app):
    app._consecutive_resets = 2
    app.transcriber = mock.Mock()
    app.transcriber.transcribe.return_value = ("hello world", None)
    with mock.patch("main._copy_to_clipboard"), mock.patch("main._paste_from_clipboard"):
        app._do_transcribe(b"x" * 2000)
    assert app._consecutive_resets == 0


def test_chord_fires_toggle_once():
    import hotkey
    cb = mock.Mock()
    hk = hotkey.HotkeyListener(on_toggle=cb)
    hk._dispatch = lambda fn: fn() if fn else None
    # cmd alone → no chord
    hk._rcmd_down = True
    hk._check_lock_chord()
    cb.assert_not_called()
    # add shift → chord fires exactly once (latched)
    hk._rshift_down = True
    hk._check_lock_chord()
    hk._check_lock_chord()
    cb.assert_called_once()
    # release shift then re-press → fires again
    hk._rshift_down = False
    hk._check_lock_chord()
    hk._rshift_down = True
    hk._check_lock_chord()
    assert cb.call_count == 2


# --- accidental-tap cancel (issue #327) ------------------------------------


def test_cancel_stops_a_hold_recording(app):
    """THE BUG: a sub-threshold tap opened the mic and nothing ever closed it."""
    app._on_hotkey_press()              # tap starts a hold recording
    assert app._recording is True
    app._on_hotkey_cancel()             # released too fast to be a real hold
    assert app._recording is False
    assert app._mode is None
    app.recorder.stop.assert_called_once()


def test_cancel_does_not_transcribe(app):
    """An accidental tap is not speech — no transcription thread may spawn."""
    app._on_hotkey_press()
    app.transcriber.transcribe.reset_mock()
    app._on_hotkey_cancel()
    app.transcriber.transcribe.assert_not_called()


def test_cancel_leaves_locked_recording_alone(app):
    """Hands-free is deliberate; it stops only on the next toggle."""
    app._on_hotkey_toggle()             # start locked
    app.recorder.stop.reset_mock()
    app._on_hotkey_cancel()             # stray short tap
    assert app._recording is True
    assert app._mode == "locked"
    app.recorder.stop.assert_not_called()


def test_cancel_when_idle_is_a_noop(app):
    app._on_hotkey_cancel()
    assert app._recording is False
    app.recorder.stop.assert_not_called()


def test_cancel_survives_a_failing_recorder_stop(app):
    """A mic teardown error must not escape into the hotkey thread."""
    app._on_hotkey_press()
    app.recorder.stop.side_effect = RuntimeError("device vanished")
    app._on_hotkey_cancel()             # must not raise
    assert app._recording is False


def test_no_path_leaves_recorder_running_after_a_tap(app):
    """The #327 invariant, stated directly."""
    app._on_hotkey_press()
    app._on_hotkey_cancel()
    assert app._recording is False
    assert app._mode is None
