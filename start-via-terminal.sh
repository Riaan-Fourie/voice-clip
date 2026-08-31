#!/bin/bash
# Launch VoiceClip via Terminal.app so CGEventTap inherits Accessibility permission.
# Designed to be called from a LaunchAgent at login.
#
# The trailing `exit` ends the shell when VoiceClip stops, which is what makes the
# window closeable at all — Terminal refuses to close a window whose shell is still
# alive. The window itself is then closed by the watchdog's reaper (#407).

DIR="$(cd "$(dirname "$0")" && pwd)"

osascript -e "
tell application \"Terminal\"
    do script \"'$DIR/scripts/run-voiceclip.sh'; exit\"
end tell
"
