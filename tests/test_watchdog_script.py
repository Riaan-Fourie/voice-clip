"""Runs the shell-level watchdog cases (issue #333) as part of the pytest suite.

`scripts/watchdog.sh` is the last line of defence — the thing that recovers VoiceClip
when the process is alive but useless. It went years untested and untracked, and its
single health check (`ps -p <pid>`) missed every serious failure the app has ever had.
Now it gets exercised like anything else.

The cases live in `tests/watchdog_sh_cases.sh` because they need a sandboxed `$HOME`,
fake app processes and a stubbed `osascript` — all far more natural in bash than in
pytest. This module just runs them and surfaces the output on failure.
"""

import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "watchdog_sh_cases.sh")
WATCHDOG = os.path.join(os.path.dirname(HERE), "scripts", "watchdog.sh")


def test_watchdog_script_exists_and_parses():
    assert os.path.exists(WATCHDOG), "scripts/watchdog.sh is missing"
    subprocess.run(["bash", "-n", WATCHDOG], check=True)


def test_watchdog_script_health_checks():
    """Existence AND liveness, plus the three ways a naive fix would misfire:
    a leftover heartbeat from another pid, a build with no heartbeat at all, and
    killing a wedged process while in the relaunch cooldown (which would turn a
    recoverable wedge into an outage)."""
    proc = subprocess.run(["bash", CASES, WATCHDOG], capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, f"watchdog.sh cases failed:\n{proc.stdout}\n{proc.stderr}"
    assert "failed=0" in proc.stdout, proc.stdout
