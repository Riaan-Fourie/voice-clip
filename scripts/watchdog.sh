#!/bin/bash
# VoiceClip watchdog — keeps the app not just RUNNING but RESPONSIVE.
#
# Deployed to ~/.voice-clip/watchdog.sh and fired every 120s by the
# com.riaanfourie.voiceclip-watchdog LaunchAgent. Install with:
#     scripts/install-watchdog.sh
#
# Terminal launch is deliberate: CGEventTap needs Accessibility, which the python
# process inherits from Terminal.app. That puts the process outside launchd's
# supervision (com.riaanfourie.voiceclip only fires at login), so this polling job
# fills the KeepAlive gap. VoiceClip's own single-instance lock makes a
# false-positive relaunch harmless.
#
# TWO health checks, because "the process exists" has now twice been the wrong one:
#
#   1. EXISTENCE — pidfile names a live `main.py`. Catches a crash.
#   2. LIVENESS  — the heartbeat file, stamped from VoiceClip's MAIN run loop, is
#                  fresh. Catches the far nastier failure where the process is alive
#                  and `ps` looks perfect but the run loop (and with it the
#                  CGEventTap) is wedged. In #333 a CoreAudio deadlock left VoiceClip
#                  "running" and useless for 31 minutes; this watchdog checked only
#                  (1), saw a healthy process, and never fired.

set -uo pipefail

DIR="/Users/riaanfourie/Personal Projects/Jarvis/Jarvis/repos/voice-clip"
STATE="$HOME/.voice-clip"
PIDFILE="$STATE/voiceclip.pid"
HEARTBEAT="$STATE/heartbeat"
STAMP="$STATE/watchdog.laststart"

# A heartbeat older than this means the run loop is wedged, not just busy. The app
# stamps it every 5s, so 90s is ~18 missed beats — far outside any legitimate pause
# (incl. the re-exec + cold model reload) and well inside the 120s poll interval.
STALE_AFTER=90

# Don't spawn a Terminal window every poll if the app is crash-looping.
RELAUNCH_COOLDOWN=600

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$STATE/watchdog.log"; }

# Echo the live VoiceClip pid, or return 1 if it isn't running.
running_pid() {
  [ -f "$PIDFILE" ] || return 1
  local pid
  pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
  [ -n "$pid" ] || return 1
  ps -p "$pid" -o command= 2>/dev/null | grep -q "main.py" || return 1
  echo "$pid"
}

# True if we are outside the relaunch cooldown. Checked BEFORE killing a wedged
# process — killing without relaunching would turn a wedge into an outage.
may_relaunch() {
  [ -f "$STAMP" ] || return 0
  [ $(( $(date +%s) - $(stat -f %m "$STAMP") )) -ge "$RELAUNCH_COOLDOWN" ]
}

relaunch() {
  touch "$STAMP"
  # stderr redirect is the crash evidence we've never had: deaths so far leave
  # no trace in voiceclip-debug.log (it just stops).
  osascript >/dev/null <<EOF
tell application "Terminal"
    do script "cd '$DIR' && ./venv/bin/python main.py 2>> '$STATE/stderr.log'; exit"
end tell
EOF
}

# --- 1. EXISTENCE ------------------------------------------------------------
if ! pid=$(running_pid); then
  may_relaunch || exit 0
  log "voiceclip not running — relaunching"
  relaunch
  exit 0
fi

# --- 2. LIVENESS -------------------------------------------------------------
# No heartbeat file yet = a build older than #333, or a process still starting.
# Absence is not evidence of a wedge, so leave it alone.
[ -f "$HEARTBEAT" ] || exit 0

read -r hb_ts hb_pid _ < "$HEARTBEAT" || exit 0
[ -n "${hb_ts:-}" ] && [ -n "${hb_pid:-}" ] || exit 0

# A heartbeat from a DIFFERENT pid is a leftover from the previous run (or a
# just-completed re-exec). The live process will stamp its own within 5s — judging
# it on someone else's timestamp would kill a perfectly healthy app.
[ "$hb_pid" = "$pid" ] || exit 0

age=$(( $(date +%s) - hb_ts ))
[ "$age" -gt "$STALE_AFTER" ] || exit 0

if ! may_relaunch; then
  log "voiceclip pid $pid wedged (heartbeat ${age}s stale) — in relaunch cooldown, leaving it"
  exit 0
fi

log "voiceclip pid $pid ALIVE BUT WEDGED (heartbeat ${age}s stale) — killing and relaunching"
# SIGTERM is not enough: a wedged run loop never runs Python's signal handler, so
# the polite signal is simply ignored (witnessed in #333). Go straight to SIGKILL.
kill -9 "$pid" 2>/dev/null
sleep 2
relaunch
