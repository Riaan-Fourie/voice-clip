"""Manual integration check for the CoreAudio-wedge recovery (issue #333).

The unit tests in test_audio_wedge.py monkeypatch `_reexec`, so they prove the
DECISION logic but never actually recover anything. This script injects a real,
genuinely unkillable hang — a thread parked forever inside `_audio_call()` while
holding `_rec_lock`, which is precisely the shape of the 2026-08-11 incident — and
witnesses the REAL watchdog performing a REAL `os.execv` to escape it:

    ./venv/bin/python tests/manual_audio_wedge.py

Sequence:
  1. gen 0 parks a thread in `_audio_call("recorder.stop")` holding `_rec_lock`,
     and asserts the lock really is unobtainable (i.e. the hotkey path IS dead —
     the same state the app sat in for 31 minutes).
  2. The real `_transcribe_watchdog` loop runs against that state.
  3. It must detect the wedge and re-exec. gen 1 prints PASS.

A Python-level `Event.wait()` stands in for the CoreAudio HAL deadlock: both are
threads that will never return on their own, which is the only property the
watchdog can act on. Nothing here can un-park that thread — if the process comes
back, it is because the watchdog genuinely replaced the process image.
"""
import os
import subprocess
import sys
import threading
import time

# Watchdog timings compressed so the witness runs in seconds, not half a minute.
TEST_STUCK_TIMEOUT = 2.0
TEST_WATCHDOG_INTERVAL = 0.25
GIVE_UP_AFTER = 20.0


def _build_wedged_app(vc):
    """A VoiceClipApp with a thread permanently parked inside a native audio call."""
    app = vc.VoiceClipApp.__new__(vc.VoiceClipApp)
    app._rec_lock = threading.RLock()
    app._audio_call_name = None
    app._audio_call_started_at = 0.0
    app._lock_timeout_logged = False
    app._transcribing = False
    app._transcribe_started_at = 0.0
    app._transcribe_thread = None
    app._consecutive_resets = 0

    parked = threading.Event()  # never set — that is the point

    def wedged_stop():
        # Exactly the #333 shape: the blocking audio call runs while _rec_lock is
        # held, so the wedge takes the whole hotkey path down with it.
        with app._rec_lock:
            with vc.VoiceClipApp._audio_call(app, "recorder.stop"):
                parked.wait()

    threading.Thread(target=wedged_stop, daemon=True).start()
    time.sleep(0.3)
    return app


def main():
    gen = int(os.environ.get("AUDIO_WEDGE_GEN", "0"))
    if gen == 1:
        print(f"gen=1: PASS — watchdog re-execed out of an unrecoverable audio wedge "
              f"(pid={os.getpid()})")
        sys.exit(0)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import main as vc

    vc.AUDIO_CALL_STUCK_TIMEOUT = TEST_STUCK_TIMEOUT
    vc.TRANSCRIBE_WATCHDOG_INTERVAL = TEST_WATCHDOG_INTERVAL

    app = _build_wedged_app(vc)
    print(f"gen=0: parked a thread inside recorder.stop (pid={os.getpid()})", flush=True)

    # Witness the damage first — otherwise we'd be testing recovery from nothing.
    assert not app._rec_lock.acquire(timeout=1.0), \
        "_rec_lock was obtainable — the wedge did not reproduce"
    wedged_for = vc.VoiceClipApp._audio_wedge_seconds(app)
    assert wedged_for > 0, "the audio call is not visible to the watchdog"
    print(f"gen=0: _rec_lock is unobtainable and the call has been in flight "
          f"{wedged_for:.1f}s — the hotkey path is dead, exactly as in the incident",
          flush=True)

    # The real _reexec, but tagged so the replacement image can identify itself.
    def reexec():
        print("gen=0: watchdog is re-execing NOW", flush=True)
        sys.stdout.flush()
        os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)],
                  dict(os.environ, AUDIO_WEDGE_GEN="1"))

    app._reexec = reexec

    threading.Thread(target=vc.VoiceClipApp._transcribe_watchdog,
                     args=(app,), daemon=True).start()

    time.sleep(GIVE_UP_AFTER)
    print(f"gen=0: FAIL — still wedged after {GIVE_UP_AFTER:.0f}s; watchdog never re-execed")
    sys.exit(2)


if __name__ == "__main__":
    if os.environ.get("AUDIO_WEDGE_WRAPPED"):
        main()
    else:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__)],
            env=dict(os.environ, AUDIO_WEDGE_WRAPPED="1"),
            capture_output=True,
            text=True,
            timeout=GIVE_UP_AFTER + 30,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        assert proc.returncode == 0, f"audio-wedge witness FAILED (rc={proc.returncode})"
        assert "PASS" in proc.stdout, "expected a PASS line from the re-exec'd image"
        print("\nmanual_audio_wedge: WITNESSED real watchdog recovery from a real hang ✓")
