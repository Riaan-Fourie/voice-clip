"""Manual integration check for the watchdog re-exec recovery (issue #187).

The unit tests in test_transcribe_wedge.py monkeypatch `_reexec`, so they prove the
decision logic but NOT the real `os.execv` + `O_CLOEXEC` instance-lock behaviour.
This script witnesses the actual process replacement end-to-end:

    ./venv/bin/python tests/manual_reexec.py

It exercises exactly the sequence `_reexec` relies on:
  1. Acquire an flock on a lock fd opened with the SAME flags main.py uses
     (O_RDWR | O_CREAT | O_CLOEXEC).
  2. os.execv this script with a generation counter bumped in the env.
  3. The re-exec'd image must RE-ACQUIRE the same flock — which only works if the
     exec released the parent's lock (i.e. O_CLOEXEC did its job). Without O_CLOEXEC
     the second acquire blocks/fails and the app would exit as a false "duplicate".

PASS is printed by generation 1 once it re-acquires the lock; the wrapper asserts it.
"""
import fcntl
import os
import subprocess
import sys
import tempfile

LOCK_PATH = os.path.join(tempfile.gettempdir(), "voiceclip-reexec-witness.lock")


def _open_and_lock():
    """Open the lock fd with main.py's exact flags and take an exclusive flock.
    Returns the fd on success, or None if the lock is already held."""
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def main():
    gen = int(os.environ.get("REEXEC_GEN", "0"))

    fd = _open_and_lock()
    if fd is None:
        # Lock still held → the exec did NOT release it. This is the failure the
        # O_CLOEXEC flag exists to prevent.
        print(f"gen={gen}: FAIL — lock still held after exec (O_CLOEXEC missing?)")
        sys.exit(2)

    print(f"gen={gen}: acquired lock (pid={os.getpid()})", flush=True)

    if gen == 0:
        # First image: re-exec ourselves, exactly like VoiceClipApp._reexec does.
        env = dict(os.environ, REEXEC_GEN="1")
        print(f"gen=0: re-execing (pid stays {os.getpid()})...", flush=True)
        os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)], env)
        # unreachable
    else:
        # Second image: we got the lock back across the exec. That's the witness.
        print(f"gen={gen}: PASS — re-exec released + re-acquired the instance lock")
        sys.exit(0)


if __name__ == "__main__":
    # Direct run = the generation-0 entry point. The wrapper below verifies the
    # gen-1 child prints PASS and exits 0.
    if os.environ.get("REEXEC_WITNESS_WRAPPED"):
        main()
    else:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__)],
            env=dict(os.environ, REEXEC_WITNESS_WRAPPED="1"),
            capture_output=True,
            text=True,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        assert proc.returncode == 0, f"re-exec witness FAILED (rc={proc.returncode})"
        assert "PASS" in proc.stdout, "expected a PASS line from the re-exec'd image"
        print("\nmanual_reexec: WITNESSED real os.execv + O_CLOEXEC lock re-acquire ✓")
