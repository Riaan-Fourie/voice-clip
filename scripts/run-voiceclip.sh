#!/bin/bash
# Run VoiceClip in the foreground of a Terminal window. Launched by
# start-via-terminal.sh (login) and by watchdog.sh's relaunch().
#
# VoiceClip must be started from Terminal.app: CGEventTap needs Accessibility, which
# the python process inherits from Terminal (see GOTCHAS.md). The cost is that
# `do script` always opens a NEW window and Terminal keeps it after the shell exits,
# so before #407 every launch left a "[Process completed]" corpse behind — and macOS
# restored each corpse at the next login, so they compounded. 9 dead windows and zero
# running processes, witnessed 2026-08-31.
#
# This script does not close its own window: it cannot. Terminal ignores `close` on a
# window whose shell is still alive, and once the shell HAS exited there is nothing
# left in the window to run the close. Reaping is therefore the watchdog's job — see
# reap_dead_windows() in watchdog.sh. All this script owes it is the marker below.

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$HOME/.voice-clip"

mkdir -p "$STATE"

# How the reaper tells our windows apart from Riaan's own. Matching on the title
# instead would be guesswork: a corpse restored at login is titled plain "-zsh",
# exactly like every other idle window.
echo "VOICECLIP-RUNNER-WINDOW"

cd "$DIR" || exit 1
# stderr redirect is the crash evidence we've never had: deaths so far leave no trace
# in voiceclip-debug.log, it just stops.
exec ./venv/bin/python main.py 2>> "$STATE/stderr.log"
