#!/bin/bash
# VoiceClip watchdog — keeps the app not just RUNNING but RESPONSIVE.
#
# Deployed to ~/.voice-clip/watchdog.sh and fired every 120s by the
# com.riaanfourie.voiceclip-watchdog LaunchAgent. Install with:
#     scripts/install-watchdog.sh
#
# launchd starts VoiceClip through the launcher app (scripts/install-launcher.sh)
# and restarts it after a crash. This job adds what launchd cannot see: a clean
# exit it was told to leave alone, and a process that is alive but wedged.
# VoiceClip's own single-instance lock makes a false-positive relaunch harmless.
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

AGENT_LABEL="com.riaanfourie.voiceclip"
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

# Close the Terminal windows the old Terminal launch left behind (#407). VoiceClip
# now starts headless through launchd, so this only clears windows that predate it,
# including ones macOS restores at login.
#
# Terminal keeps a window after its shell exits, showing "[Process completed]", and
# macOS restores every one of those at the next login — so they compound. By
# 2026-08-31 there were 9 corpses and zero running processes.
#
# The window cannot close itself: Terminal ignores `close` while the shell is alive,
# and after the shell exits there is nothing left in the window to run the close. So
# the reaping lands here, on the 120s poll.
#
# Three things this got wrong before and must not get wrong again:
#
#   1. `close` is a no-op unless Terminal is activated. Witnessed repeatedly during
#      #407: the command returns success, no error, and the window stays. So we scan
#      first WITHOUT activating (reading the window list is fine unfocused) and only
#      steal focus when there is actually something to close — which, after this fix,
#      means a login restore or a stop-without-relaunch. Rare, and self-limiting.
#   2. Never close by an id read earlier. Terminal's window list goes stale within
#      seconds; during #407 a close aimed at a dead window's id hit the LIVE VoiceClip
#      window instead and killed the app. Close the object reference from the same
#      iteration that tested it.
#   3. `busy is false` is a hard guard, not an optimisation — it is what keeps a
#      running VoiceClip (or any window Riaan has a process in) out of the loop.
#
# The old runner script printed the scrollback marker. Matching on the title instead
# would be guesswork: a restored corpse is titled plain "-zsh", like any idle window.
MARKER="VOICECLIP-RUNNER-WINDOW"

count_dead_windows() {
  osascript 2>/dev/null <<EOF
tell application "Terminal"
    set n to 0
    repeat with w in (every window whose busy is false)
        try
            if (history of selected tab of w) contains "$MARKER" then set n to n + 1
        end try
    end repeat
    return n
end tell
EOF
}

reap_dead_windows() {
  # Telling Terminal anything launches it. VoiceClip no longer runs in Terminal, so
  # only look for leftover windows when Terminal is already open.
  pgrep -xq Terminal || return 0
  local n
  n=$(count_dead_windows)
  [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null || return 0

  log "reaping $n dead VoiceClip Terminal window(s)"
  osascript >/dev/null 2>&1 <<EOF
tell application "Terminal"
    activate
    delay 0.3
    repeat with w in (every window whose busy is false)
        try
            if (history of selected tab of w) contains "$MARKER" then close w saving no
        end try
    end repeat
end tell
EOF
}

relaunch() {
  touch "$STAMP"
  # launchd owns the process now (#407): -k stops any instance it still holds, then
  # starts the launcher again. No Terminal window.
  launchctl kickstart -k "gui/$(id -u)/$AGENT_LABEL"
}

reap_dead_windows

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

# Sleep is not a wedge (#630). The heartbeat timer cannot fire while the Mac is
# asleep, so after a long lid-closed sleep the stamp is as old as the sleep. A
# DarkWake (a ~2s maintenance wake, lid still shut) runs this job before the app has
# stamped again: on 2026-10-12 that killed a healthy app at 18:19 with the heartbeat
# "997s stale", every second of it asleep. So only time AWAKE counts: measure from
# the later of the last stamp and the last wake, which kern.waketime records for
# DarkWakes too. A wedge that spans a sleep is still caught once the Mac has been
# awake for STALE_AFTER.
wake_ts=$(sysctl -n kern.waketime 2>/dev/null | sed -nE 's/.*sec = ([0-9]+),.*/\1/p')
since=$hb_ts
[ -n "$wake_ts" ] && [ "$wake_ts" -gt "$since" ] 2>/dev/null && since=$wake_ts

age=$(( $(date +%s) - since ))
[ "$age" -gt "$STALE_AFTER" ] || exit 0

if ! may_relaunch; then
  log "voiceclip pid $pid wedged (no heartbeat for ${age}s awake) — in relaunch cooldown, leaving it"
  exit 0
fi

log "voiceclip pid $pid ALIVE BUT WEDGED (no heartbeat for ${age}s awake) — killing and relaunching"
# SIGTERM is not enough: a wedged run loop never runs Python's signal handler, so
# the polite signal is simply ignored (witnessed in #333). Go straight to SIGKILL.
kill -9 "$pid" 2>/dev/null
sleep 2
relaunch
