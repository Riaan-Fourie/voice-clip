#!/usr/bin/env python3
"""
VoiceClip — Hold-to-record speech-to-text for macOS.
Hold Right Command key to record, release to transcribe, result auto-pastes.

Uses CGEventTap (passive/listen-only) for hotkey detection.
Requires Accessibility (hotkey) and Microphone permissions.
"""

import atexit
import fcntl
import os
import subprocess
import threading
import time
import logging
import sys

import rumps

from recorder import (
    Recorder,
    MIC_AIRPODS,
    MIC_MACBOOK,
    MIC_SYSTEM,
    MIC_PREFERENCES,
    DEFAULT_MIC_PREFERENCE,
)
from transcriber import Transcriber
from overlay import RecordingOverlay
from hotkey import HotkeyListener
from transition import (
    TransitionManager,
    MODE_FADE,
    MODE_PAUSE,
    MODE_OFF,
    TRANSITION_MODES,
    DEFAULT_TRANSITION_MODE,
)
from utils import _log, STATE_DIR, LOG_PATH, load_settings, save_settings

# Menu labels for the Microphone submenu, in display order.
MIC_MENU_LABELS = {
    MIC_AIRPODS: "AirPods",
    MIC_MACBOOK: "MacBook Mic",
    MIC_SYSTEM: "System Default",
}

# Menu labels for the Transition submenu (how to mask the Bluetooth HFP flip
# when recording starts on AirPods), in display order.
TRANSITION_MENU_LABELS = {
    MODE_FADE: "Volume Fade",
    MODE_PAUSE: "Auto-Pause Music",
    MODE_OFF: "Off",
}

PID_FILE = os.path.join(STATE_DIR, "voiceclip.pid")
_instance_lock_fd = None

# If a transcription has held the `_transcribing` gate longer than this, assume it
# hung. small.en on this Mac transcribes in well under a second; 30s is far beyond
# any real run (incl. the first cold model load). Recovery depends on whether the
# worker thread is still alive: a leaked gate just needs a reset (issue #170), but a
# thread genuinely wedged inside the native mlx_whisper/Metal call needs a full
# process re-exec — no in-process reset can unwedge the GPU (jarvis-system #6).
TRANSCRIBE_STUCK_TIMEOUT = 30.0

# How often the background watchdog checks for a wedged transcription. Kept short so
# recovery is automatic within ~5s of crossing the timeout, with no user keypress.
TRANSCRIBE_WATCHDOG_INTERVAL = 5.0

os.makedirs(STATE_DIR, exist_ok=True)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s %(message)s",
)
log = logging.getLogger("voiceclip")


class VoiceClipApp(rumps.App):
    def __init__(self):
        super().__init__(
            name="VoiceClip",
            icon=None,
            title="\U0001f399",  # 🎙
        )
        _log("init: creating overlay")
        self.overlay = RecordingOverlay()
        _log("init: creating recorder")
        settings = load_settings()
        mic_pref = settings.get("mic_preference", DEFAULT_MIC_PREFERENCE)
        if mic_pref not in MIC_PREFERENCES:
            mic_pref = DEFAULT_MIC_PREFERENCE
        transition_mode = settings.get("transition_mode", DEFAULT_TRANSITION_MODE)
        if transition_mode not in TRANSITION_MODES:
            transition_mode = DEFAULT_TRANSITION_MODE
        self.transition = TransitionManager(mode=transition_mode)
        self.recorder = Recorder(
            on_level=self._on_audio_level,
            mic_preference=mic_pref,
            hooks=self.transition,
        )
        _log("init: creating transcriber")
        self.transcriber = Transcriber()
        _log("init: transcriber done")
        self._key_pressed = False
        self._transcribing = False
        self._transcribe_started_at = 0.0  # monotonic ts; backstop for a hung transcribe
        self._transcribe_thread = None  # the in-flight worker; lets the watchdog tell a
        #                                 true native wedge (alive) from a leaked gate (dead)

        # Build menu — Microphone submenu holds one check-marked item per
        # preference; picking one persists to ~/.voice-clip/settings.json.
        self._mic_items = {}
        mic_menu = rumps.MenuItem("Microphone")
        for pref in MIC_PREFERENCES:
            item = rumps.MenuItem(MIC_MENU_LABELS[pref], callback=self._on_mic_select)
            item.state = 1 if pref == mic_pref else 0
            self._mic_items[pref] = item
            mic_menu.add(item)
        self._transition_items = {}
        transition_menu = rumps.MenuItem("Transition")
        for mode in TRANSITION_MODES:
            item = rumps.MenuItem(
                TRANSITION_MENU_LABELS[mode], callback=self._on_transition_select
            )
            item.state = 1 if mode == transition_mode else 0
            self._transition_items[mode] = item
            transition_menu.add(item)
        self.menu = [
            rumps.MenuItem("Status: Ready", callback=None),
            None,  # separator
            mic_menu,
            transition_menu,
            None,
            rumps.MenuItem("Retry Failed (0)", callback=self._retry_failed),
            None,
        ]
        self._update_failed_count()

        # Start hotkey listener (CGEventTap — passive, Accessibility only)
        log.info("Starting VoiceClip — registering CGEventTap hotkey listener")
        self._hotkey = HotkeyListener(
            on_press=self._on_hotkey_press,
            on_release=self._on_hotkey_release,
        )
        self._hotkey.start()
        log.info("Hotkey listener started")

        # Background watchdog: re-exec the process if a transcription hangs inside the
        # native mlx_whisper/Metal call (jarvis-system #6). The hotkey-press backstop
        # only recovers on the *next* press; this recovers with no user interaction.
        self._transcribe_watchdog_thread = threading.Thread(
            target=self._transcribe_watchdog, daemon=True
        )
        self._transcribe_watchdog_thread.start()

    def _on_hotkey_press(self):
        """Right Command pressed — start recording."""
        if self._transcribing:
            # A transcription is in flight. If it has hung past the timeout, recover
            # before starting a new recording; otherwise it's legitimately busy.
            action = self._stuck_recovery_action()
            if action == "busy":
                return
            stuck_for = time.monotonic() - self._transcribe_started_at
            if action == "reexec":
                # Thread still alive past the timeout = wedged in the native
                # mlx_whisper/Metal call. No reset can unwedge it (jarvis-system #6).
                log.error(f"Transcription wedged {stuck_for:.0f}s (on press) — re-execing")
                _log(f"Transcription wedged {stuck_for:.0f}s (on press) — re-execing")
                self._reexec()  # never returns
            else:  # "reset" — gate leaked but the worker already died (issue #170)
                log.warning(f"_transcribing gate leaked {stuck_for:.0f}s — force-resetting")
                _log(f"_transcribing gate leaked {stuck_for:.0f}s — force-resetting")
                self._transcribing = False
        if self._key_pressed:
            return
        log.info("Hotkey pressed — start recording")
        self._key_pressed = True
        self._set_status("Recording...")
        self.title = "\U0001f534"  # 🔴
        self.overlay.show()
        try:
            self.recorder.start()
        except Exception as e:
            # A dead mic must NEVER be a silent no-op (#265: five recordings
            # failed with zero user-visible signal) — and an exception must
            # never escape into the CGEventTap callback.
            log.error(f"recorder.start failed: {e}", exc_info=True)
            _log(f"recorder.start failed: {e}")
            self._key_pressed = False
            self.overlay.hide()
            self._set_status(f"Mic error: {str(e)[:40]}")
            self.title = "⚠️"  # ⚠️
            self._notify("Microphone Error", f"Could not open the mic: {str(e)[:80]}")
            threading.Timer(3.0, self._reset_status).start()
            return
        if self.recorder.last_device_name:
            self._set_status(f"Recording ({self.recorder.last_device_name})...")

    def _on_hotkey_release(self):
        """Right Command released — stop recording and transcribe."""
        if not self._key_pressed:
            return
        log.info("Hotkey released — stop recording")
        self._key_pressed = False
        self._transcribing = True
        self._transcribe_started_at = time.monotonic()
        self._set_status("Transcribing...")
        self.title = "\u23f3"  # ⏳
        self.overlay.hide()

        wav_bytes = self.recorder.stop()

        if not wav_bytes or len(wav_bytes) < 1000:
            self._set_status("Ready (recording too short)")
            self.title = "\U0001f399"  # 🎙
            self._transcribing = False
            return

        # Keep the handle so the watchdog can distinguish a true native wedge
        # (thread still alive) from a merely leaked gate (thread already gone).
        self._transcribe_thread = threading.Thread(
            target=self._safe_transcribe, args=(wav_bytes,), daemon=True
        )
        self._transcribe_thread.start()

    def _on_audio_level(self, level: float):
        """Called from recorder with current audio RMS level."""
        self.overlay.set_level(level)

    def _on_mic_select(self, sender):
        """Microphone submenu click — switch preference and persist it."""
        pref = next(
            (p for p, item in self._mic_items.items() if item is sender),
            DEFAULT_MIC_PREFERENCE,
        )
        self.recorder.mic_preference = pref
        for p, item in self._mic_items.items():
            item.state = 1 if p == pref else 0
        settings = load_settings()
        settings["mic_preference"] = pref
        save_settings(settings)
        log.info(f"Mic preference set to {pref}")
        self._set_status(f"Mic: {MIC_MENU_LABELS[pref]}")
        threading.Timer(3.0, self._reset_status).start()

    def _on_transition_select(self, sender):
        """Transition submenu click — switch HFP-flip masking mode, persist."""
        mode = next(
            (m for m, item in self._transition_items.items() if item is sender),
            DEFAULT_TRANSITION_MODE,
        )
        self.transition.mode = mode
        for m, item in self._transition_items.items():
            item.state = 1 if m == mode else 0
        settings = load_settings()
        settings["transition_mode"] = mode
        save_settings(settings)
        log.info(f"Transition mode set to {mode}")
        self._set_status(f"Transition: {TRANSITION_MENU_LABELS[mode]}")
        threading.Timer(3.0, self._reset_status).start()

    def _do_transcribe(self, wav_bytes: bytes):
        """Run transcription and copy result to clipboard."""
        text, error = self.transcriber.transcribe(wav_bytes)

        if error:
            log.error(f"Transcription error: {error}")
            self._set_status(f"Error: {error[:40]}...")
            self.title = "\u26a0\ufe0f"  # ⚠️
            self._update_failed_count()
            self._notify("Transcription Failed", f"Saved for retry. Error: {error[:60]}")
            threading.Timer(3.0, self._reset_status).start()
        elif text:
            log.info(f"Transcribed: {text[:80]}")
            _copy_to_clipboard(text)
            _paste_from_clipboard()
            self._set_status(f"Pasted: {text[:50]}...")
            self.title = "\u2705"  # ✅
            self._notify("Copied to Clipboard", text[:100])
            threading.Timer(3.0, self._reset_status).start()
        else:
            self._set_status("No speech detected")
            self.title = "\U0001f399"  # 🎙

    def _safe_transcribe(self, wav_bytes: bytes):
        """Thread entry point that GUARANTEES `_transcribing` is cleared.

        `_do_transcribe` runs on this daemon thread and touches AppKit/rumps
        (self.title, _set_status, _notify) off the main thread — any of which can
        raise. If the flag stayed True after such a raise, `_on_hotkey_press` would
        hit `if self._transcribing: return` and silently ignore every future press,
        leaving the app dead-while-running with no log of why (issue #170). The
        `finally` here is the single guarantee that one bad transcription cannot
        permanently wedge dictation.
        """
        try:
            self._do_transcribe(wav_bytes)
        except Exception as e:
            log.error(f"_do_transcribe crashed: {e}", exc_info=True)
            _log(f"_do_transcribe crashed: {e}")
            try:
                self.title = "\U0001f399"  # 🎙
            except Exception:
                pass
        finally:
            self._transcribing = False

    def _stuck_recovery_action(self) -> str:
        """Classify the in-flight transcription so press-path and watchdog agree.

        Returns:
            "busy"   — not stuck (no transcription, or still within the timeout).
            "reset"  — gate held past the timeout but the worker thread is already
                       gone: a cheap flag reset is enough (the leaked-gate case the
                       #170 `finally` is meant to prevent, kept as belt-and-braces).
            "reexec" — gate held past the timeout AND the worker thread is still
                       alive: it's wedged inside the native mlx_whisper/Metal call,
                       which nothing in-process can unwedge (jarvis-system #6). The
                       process must be replaced.
        """
        if not self._transcribing:
            return "busy"
        if time.monotonic() - self._transcribe_started_at <= TRANSCRIBE_STUCK_TIMEOUT:
            return "busy"
        t = self._transcribe_thread
        if t is not None and t.is_alive():
            return "reexec"
        return "reset"

    def _transcribe_watchdog(self):
        """Daemon loop: auto-recover a transcription wedged in native code.

        The #170 `finally` + the hotkey-press backstop clear the gate when the
        transcribe thread *raises*. But a hang *inside* `mlx_whisper.transcribe`
        (native Metal) never returns, so the thread stays alive and no in-process
        reset can unwedge the GPU — every later press logs PRESS/RELEASE with no
        Transcribing line until a manual restart (jarvis-system #6, 2026-06-30).
        This loop detects that case and re-execs, with no user keypress required.

        It can run *because* a native Metal hang releases the GIL — confirmed by the
        incident log still recording hotkey events while transcription was wedged.
        """
        while True:
            time.sleep(TRANSCRIBE_WATCHDOG_INTERVAL)
            try:
                action = self._stuck_recovery_action()
                if action == "busy":
                    continue
                stuck_for = time.monotonic() - self._transcribe_started_at
                if action == "reexec":
                    log.error(f"Transcription wedged {stuck_for:.0f}s — re-execing to recover")
                    _log(f"Transcription wedged {stuck_for:.0f}s — re-execing to recover")
                    self._reexec()  # never returns
                else:  # "reset"
                    log.warning(f"_transcribing gate leaked {stuck_for:.0f}s — resetting")
                    _log(f"_transcribing gate leaked {stuck_for:.0f}s — resetting")
                    self._transcribing = False
            except Exception as e:
                log.error(f"transcribe watchdog error: {e}", exc_info=True)

    def _reexec(self):
        """Replace this wedged process image with a fresh one — the automatic
        equivalent of the manual kill+restart that has always been the only fix.

        A hang in mlx_whisper's native Metal call can't be unwedged in-process:
        Python can't kill the worker thread, clearing the gate just piles the next
        recording behind the same stuck GPU queue, and a new Transcriber reuses
        mlx_whisper's process-global model singleton. `os.execv` keeps the PID but
        discards ALL in-process state, so the fresh image re-loads the model and
        re-attaches the CGEventTap from clean. The instance-lock fd is O_CLOEXEC, so
        exec releases the flock and the new image re-acquires it. There is
        deliberately no LaunchAgent (it breaks the CGEventTap), so self-relaunch via
        execv — not self-exit — is the only crash-free recovery path.
        """
        _log("re-exec: replacing process image to recover wedged transcription")
        log.error("re-exec: replacing process image to recover wedged transcription")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _retry_failed(self, sender):
        """Retry all failed transcriptions."""
        count = self.transcriber.get_failed_count()
        if count == 0:
            self._notify("No Failed Recordings", "Nothing to retry.")
            return

        self._set_status(f"Retrying {count} failed...")
        self.title = "\u23f3"  # ⏳

        def _do_retry():
            results = self.transcriber.retry_failed()
            successes = [r for r in results if r[1] is not None]
            if successes:
                all_text = "\n".join(r[1] for r in successes)
                _copy_to_clipboard(all_text)
                self._notify(
                    f"Retried: {len(successes)}/{len(results)} succeeded",
                    all_text[:100],
                )
            else:
                self._notify("Retry Failed", "All transcriptions failed again.")
            self._update_failed_count()
            self._reset_status()

        threading.Thread(target=_do_retry, daemon=True).start()

    def _set_status(self, text):
        """Update the status menu item."""
        for key in self.menu:
            item = self.menu[key]
            if isinstance(item, rumps.MenuItem) and str(item.title).startswith("Status"):
                item.title = f"Status: {text}"
                break

    def _reset_status(self):
        self._set_status("Ready")
        self.title = "\U0001f399"  # 🎙

    def _update_failed_count(self):
        """Update the retry menu item with current failed count."""
        count = self.transcriber.get_failed_count()
        for key in self.menu:
            item = self.menu[key]
            if isinstance(item, rumps.MenuItem) and "Retry" in str(item.title):
                item.title = f"Retry Failed ({count})"
                break

    def _notify(self, title, message):
        """Show macOS notification."""
        rumps.notification(
            title="VoiceClip",
            subtitle=title,
            message=message,
            sound=False,
        )


def _copy_to_clipboard(text: str):
    """Copy text to macOS clipboard via pbcopy."""
    process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    process.communicate(text.encode("utf-8"))


def _paste_from_clipboard():
    """Simulate Cmd+V to paste into the focused window."""
    import Quartz
    import time
    time.sleep(0.1)  # small delay to ensure clipboard is ready
    # Key code 9 = 'v'
    event_down = Quartz.CGEventCreateKeyboardEvent(None, 9, True)
    event_up = Quartz.CGEventCreateKeyboardEvent(None, 9, False)
    # Set Command flag
    Quartz.CGEventSetFlags(event_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(event_up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_up)


def _check_single_instance():
    """Ensure only one VoiceClip instance is active using an advisory file lock."""
    global _instance_lock_fd
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    # O_CLOEXEC so a watchdog re-exec (os.execv) releases this flock as the old
    # image is torn down — letting the fresh image re-acquire it. Without it the
    # re-exec'd process would see the lock still held and exit as a "duplicate".
    _instance_lock_fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(_instance_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False

    os.ftruncate(_instance_lock_fd, 0)
    os.write(_instance_lock_fd, f"{os.getpid()}\n".encode("utf-8"))

    def _release_instance_lock():
        global _instance_lock_fd
        if _instance_lock_fd is not None:
            try:
                fcntl.flock(_instance_lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(_instance_lock_fd)
            _instance_lock_fd = None

    atexit.register(_release_instance_lock)
    return True


if __name__ == "__main__":
    try:
        _log("main: starting VoiceClipApp")
        if not _check_single_instance():
            _log("main: another VoiceClip instance is already running; exiting")
            sys.exit(0)
        app = VoiceClipApp()
        _log("main: calling app.run()")
        app.run()
    except Exception as e:
        _log(f"FATAL: {e}")
        import traceback
        _log(traceback.format_exc())
        raise
