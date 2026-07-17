"""Audio recorder using sounddevice — captures mic input to a WAV buffer."""

import io
import wave
import threading
import numpy as np
import sounddevice as sd

from utils import _log

SAMPLE_RATE = 16000  # 16kHz — good for speech recognition
CHANNELS = 1
DTYPE = "int16"
BLOCKSIZE = 1024

# Microphone preference values. "airpods" is the default (Riaan dictates on
# AirPods most of the day); note AirPods record over Bluetooth HFP, which is
# narrowband — transcription quality is measurably worse than the built-in mic
# (see GOTCHAS.md). The menu toggle exists so switching back is one click.
MIC_AIRPODS = "airpods"
MIC_MACBOOK = "macbook"
MIC_SYSTEM = "system"
MIC_PREFERENCES = (MIC_AIRPODS, MIC_MACBOOK, MIC_SYSTEM)
DEFAULT_MIC_PREFERENCE = MIC_AIRPODS


def _find_input_device(devices, needle):
    """Return the index of the first input device whose name contains needle."""
    for i, dev in enumerate(devices):
        if needle.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
            return i
    return None


def resolve_input_device(preference, devices=None):
    """Map a mic preference to a sounddevice input index.

    Returns (device_index_or_None, human_name). None means "system default".
    Falls back rather than failing: airpods → macbook → system default, so a
    disconnected preferred device can never break recording.
    """
    if devices is None:
        devices = sd.query_devices()

    if preference == MIC_AIRPODS:
        idx = _find_input_device(devices, "AirPods")
        if idx is not None:
            return idx, devices[idx]["name"]
        idx = _find_input_device(devices, "MacBook")
        if idx is not None:
            return idx, devices[idx]["name"]
    elif preference == MIC_MACBOOK:
        idx = _find_input_device(devices, "MacBook")
        if idx is not None:
            return idx, devices[idx]["name"]

    return None, "System Default"


class Recorder:
    def __init__(self, on_level=None, mic_preference=DEFAULT_MIC_PREFERENCE, hooks=None):
        """
        on_level: optional callback(float) called with RMS level 0.0-1.0
                  on each audio block (for VU meter).
        mic_preference: one of MIC_PREFERENCES; resolved to a device at each
                  start() so plugging/unplugging AirPods between recordings
                  just works.
        hooks: optional object with pre_open(device_name) / post_open() /
                  pre_close() / post_close() — used by TransitionManager to
                  mask the Bluetooth HFP flip around the stream lifecycle.
                  All hook calls are best-effort and never break recording.
        """
        self._frames = []
        self._stream = None
        self._recording = False
        self._on_level = on_level
        self._lock = threading.Lock()
        self.mic_preference = mic_preference
        self.hooks = hooks
        self.last_device_name = None  # set on each start(); for status display

    def _hook(self, name, *args):
        fn = getattr(self.hooks, name, None) if self.hooks else None
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            _log(f"{name} hook failed: {e}", tag="recorder")

    @property
    def is_recording(self):
        return self._recording

    def start(self):
        """Start recording from the preferred mic (fallback: system default)."""
        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._recording = True
            try:
                device, name = resolve_input_device(self.mic_preference)
            except Exception as e:
                _log(f"mic resolve failed ({e}) — using system default", tag="recorder")
                device, name = None, "System Default"
            if device is None:
                # Name the ACTUAL default input — the transition hooks need to
                # know if it's a Bluetooth device, and the status line reads
                # better ("AirPods Pro 3" beats "System Default").
                try:
                    name = sd.query_devices(kind="input")["name"]
                except Exception:
                    pass
            self.last_device_name = name
            _log(f"recording via {name} (pref={self.mic_preference})", tag="recorder")

            # Duck volume / pause music BEFORE the stream opens — opening the
            # stream is what flips the Bluetooth link to HFP.
            self._hook("pre_open", name)

            try:
                self._stream = self._open_stream(device)
            except Exception as e:
                # Device vanished between resolve and open (AirPods disconnect
                # race) or rejected our format — retry on the system default so
                # a recording is never lost to a stale device index.
                if device is None:
                    self._recording = False
                    self._hook("post_close")  # undo pre_open masking
                    raise
                _log(f"open {name} failed ({e}) — retrying system default", tag="recorder")
                self.last_device_name = "System Default"
                try:
                    self._stream = self._open_stream(None)
                except Exception:
                    self._recording = False
                    self._hook("post_close")  # undo pre_open masking
                    raise
            self._stream.start()
            self._hook("post_open")

    def _open_stream(self, device):
        return sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCKSIZE,
            callback=self._audio_callback,
        )

    def stop(self) -> bytes:
        """Stop recording and return WAV bytes."""
        with self._lock:
            if not self._recording:
                return b""
            self._recording = False
            self._hook("pre_close")  # duck for the HFP→A2DP flip-back
            self._stream.stop()
            self._stream.close()
            self._stream = None
            self._hook("post_close")
            frames = self._frames
            self._frames = []

        # Convert frames to WAV bytes
        audio_data = np.concatenate(frames, axis=0) if frames else np.array([], dtype=DTYPE)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())
        return buf.getvalue()

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice for each audio block."""
        if not self._recording:
            return
        self._frames.append(indata.copy())

        # Calculate RMS level for VU meter
        if self._on_level:
            rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
            # Normalize to 0.0-1.0 range (int16 max = 32767)
            level = min(rms / 1500.0, 1.0)
            self._on_level(level)
