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
import logging
import sys

import rumps

from recorder import Recorder
from transcriber import Transcriber
from overlay import RecordingOverlay
from hotkey import HotkeyListener
from utils import _log, STATE_DIR, LOG_PATH

PID_FILE = os.path.join(STATE_DIR, "voiceclip.pid")
_instance_lock_fd = None

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
        self.recorder = Recorder(on_level=self._on_audio_level)
        _log("init: creating transcriber")
        self.transcriber = Transcriber()
        _log("init: transcriber done")
        self._key_pressed = False
        self._transcribing = False

        # Build menu
        self.menu = [
            rumps.MenuItem("Status: Ready", callback=None),
            None,  # separator
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

    def _on_hotkey_press(self):
        """Right Command pressed — start recording."""
        if self._key_pressed or self._transcribing:
            return
        log.info("Hotkey pressed — start recording")
        self._key_pressed = True
        self._set_status("Recording...")
        self.title = "\U0001f534"  # 🔴
        self.overlay.show()
        self.recorder.start()

    def _on_hotkey_release(self):
        """Right Command released — stop recording and transcribe."""
        if not self._key_pressed:
            return
        log.info("Hotkey released — stop recording")
        self._key_pressed = False
        self._transcribing = True
        self._set_status("Transcribing...")
        self.title = "\u23f3"  # ⏳
        self.overlay.hide()

        wav_bytes = self.recorder.stop()

        if not wav_bytes or len(wav_bytes) < 1000:
            self._set_status("Ready (recording too short)")
            self.title = "\U0001f399"  # 🎙
            self._transcribing = False
            return

        threading.Thread(
            target=self._do_transcribe, args=(wav_bytes,), daemon=True
        ).start()

    def _on_audio_level(self, level: float):
        """Called from recorder with current audio RMS level."""
        self.overlay.set_level(level)

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

        self._transcribing = False

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
    _instance_lock_fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o600)
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
