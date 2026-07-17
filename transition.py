"""Mask the Bluetooth A2DP→HFP flip when recording starts and stops.

Opening a Bluetooth mic (AirPods) renegotiates the whole link down to the
hands-free profile — playback audibly crunches with a harsh clip, then pops
back ~1s after the mic closes. The flip is OS-level and cannot be removed
(Jarvis #190 was reverted over it); this module only softens it, two ways:

  fade  — duck output volume just before the mic opens (the flip lands in
          near-silence), ramp back up after; duck again around close.
  pause — pause Spotify/Music if playing, resume after the link flips back:
          clean silence instead of crushed audio.

Everything here is best-effort: an osascript failure must never delay,
break, or abort a recording.
"""

import subprocess
import threading

from utils import _log

MODE_FADE = "fade"
MODE_PAUSE = "pause"
MODE_OFF = "off"
TRANSITION_MODES = (MODE_FADE, MODE_PAUSE, MODE_OFF)
DEFAULT_TRANSITION_MODE = MODE_OFF  # opt-in only: the fade fought manual
#                                     volume changes mid-dictation (#266)

DUCK_VOLUME = 8  # output volume (0-100) while the codec flips
FADE_UP_STEPS = 4
FADE_UP_TOTAL_S = 0.4
# A2DP takes about a second to come back after the mic closes; restoring
# volume / resuming music sooner would land in the tail of the flip.
RESTORE_DELAY_S = 1.2

# Devices whose mic triggers the HFP flip — matched against the input device
# name. Wired/built-in mics never flip, so they get no masking at all.
BLUETOOTH_NAME_HINTS = ("airpods", "beats", "buds", "bluetooth", "headset")

# Media players we know how to pause/resume via AppleScript. Browser audio
# can't be scripted this way and is out of scope.
PLAYERS = ("Spotify", "Music")

_PAUSE_SCRIPT = """
set out to ""
if application "Spotify" is running then
  tell application "Spotify"
    if player state as string is "playing" then
      pause
      set out to out & "Spotify,"
    end if
  end tell
end if
if application "Music" is running then
  tell application "Music"
    if player state as string is "playing" then
      pause
      set out to out & "Music,"
    end if
  end tell
end if
return out
"""

# Save the current volume and duck in ONE osascript round-trip — this runs
# synchronously in the hotkey-press path, so every spawn counts.
_DUCK_SCRIPT = f"""
set v to output volume of (get volume settings)
if v > {DUCK_VOLUME} then set volume output volume {DUCK_VOLUME}
return v
"""


def _osascript(script, timeout=2.0):
    """Run an AppleScript snippet; None on any failure (never raises)."""
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if out.returncode != 0:
            _log(f"osascript rc={out.returncode}: {out.stderr.strip()[:120]}", tag="transition")
            return None
        return out.stdout.strip()
    except Exception as e:
        _log(f"osascript failed: {e}", tag="transition")
        return None


def _set_output_volume(v):
    _osascript(f"set volume output volume {int(v)}")


def device_needs_masking(device_name) -> bool:
    """True when recording from this device will flip the Bluetooth link."""
    if not device_name:
        return False
    name = device_name.lower()
    return any(hint in name for hint in BLUETOOTH_NAME_HINTS)


class TransitionManager:
    """Drives the masking around a recording. Hook order (from Recorder):

    pre_open(name) → mic stream opens → post_open() → ... recording ... →
    pre_close() → mic stream closes → post_close()
    """

    def __init__(self, mode=DEFAULT_TRANSITION_MODE):
        self.mode = mode
        self._saved_volume = None
        self._paused_players = []
        self._active = False  # masking engaged for the current recording
        self._restore_timer = None
        # What a cancelled restore timer still owed: ("fade", volume) or
        # ("pause", players). A quick re-press cancels the pending restore;
        # without carrying its payload forward, the pre-recording volume (or
        # the paused players) would be lost and the duck would stick.
        self._carried = None
        self._lock = threading.Lock()

    def pre_open(self, device_name):
        """Called just before the mic stream opens (synchronous, keep fast)."""
        with self._lock:
            # A quick re-press can land while the previous restore is still
            # pending; cancel it so it can't fire mid-recording, but keep
            # what it owed (see self._carried).
            if self._restore_timer is not None:
                self._restore_timer.cancel()
                self._restore_timer = None
            carried, self._carried = self._carried, None

            self._active = self.mode != MODE_OFF and device_needs_masking(device_name)
            if not self._active:
                # Mode/device changed between recordings — settle any debt now.
                if carried and carried[0] == "fade":
                    _set_output_volume(carried[1])
                elif carried and carried[0] == "pause":
                    for p in carried[1]:
                        _osascript(f'tell application "{p}" to play')
                return

            if self.mode == MODE_FADE:
                if carried and carried[0] == "fade":
                    # Volume is still ducked (or mid-ramp) from the previous
                    # recording — re-duck and keep the ORIGINAL saved volume.
                    _set_output_volume(DUCK_VOLUME)
                    self._saved_volume = carried[1]
                else:
                    out = _osascript(_DUCK_SCRIPT)
                    try:
                        self._saved_volume = int(out)
                    except (TypeError, ValueError):
                        self._saved_volume = None
                _log(f"fade: ducked from {self._saved_volume}", tag="transition")
            elif self.mode == MODE_PAUSE:
                out = _osascript(_PAUSE_SCRIPT) or ""
                paused = [p for p in out.split(",") if p in PLAYERS]
                if carried and carried[0] == "pause":
                    paused = list(dict.fromkeys(carried[1] + paused))
                self._paused_players = paused
                if paused:
                    _log(f"pause: holding {paused}", tag="transition")

    def post_open(self):
        """Called right after the stream is live — ramp volume back up."""
        if not self._active or self.mode != MODE_FADE:
            return
        saved = self._saved_volume
        if saved is None or saved <= DUCK_VOLUME:
            return

        def _ramp():
            step = (saved - DUCK_VOLUME) / FADE_UP_STEPS
            for i in range(1, FADE_UP_STEPS + 1):
                _set_output_volume(DUCK_VOLUME + step * i)
                if i < FADE_UP_STEPS:
                    threading.Event().wait(FADE_UP_TOTAL_S / FADE_UP_STEPS)

        threading.Thread(target=_ramp, daemon=True).start()

    def pre_close(self):
        """Called just before the mic stream closes — duck for the flip-back."""
        if not self._active or self.mode != MODE_FADE:
            return
        if self._saved_volume is not None and self._saved_volume > DUCK_VOLUME:
            _set_output_volume(DUCK_VOLUME)

    def post_close(self):
        """Called after the stream closed — restore volume / resume players
        once the link has flipped back to A2DP."""
        if not self._active:
            return
        with self._lock:
            self._active = False
            if self.mode == MODE_FADE and self._saved_volume is not None:
                saved = self._saved_volume
                self._saved_volume = None
                self._carried = ("fade", saved)

                def _restore():
                    _set_output_volume(saved)
                    with self._lock:
                        self._carried = None
                        self._restore_timer = None

            elif self.mode == MODE_PAUSE and self._paused_players:
                players = self._paused_players
                self._paused_players = []
                self._carried = ("pause", players)

                def _restore():
                    for p in players:
                        _osascript(f'tell application "{p}" to play')
                    _log(f"pause: resumed {players}", tag="transition")
                    with self._lock:
                        self._carried = None
                        self._restore_timer = None

            else:
                return
            timer = threading.Timer(RESTORE_DELAY_S, _restore)
            timer.daemon = True
            timer.start()
            self._restore_timer = timer
