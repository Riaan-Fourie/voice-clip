"""Audio recorder using sounddevice — captures mic input to a WAV buffer."""

import io
import os
import wave
import threading
import numpy as np
import sounddevice as sd

from utils import _log, STATE_DIR

SAMPLE_RATE = 16000  # 16kHz — good for speech recognition
CHANNELS = 1
DTYPE = "int16"
BLOCKSIZE = 1024

# Audio observability (Jarvis #277). When VOICECLIP_DEBUG_AUDIO is truthy, every
# recording logs a compact per-second RMS trace of what was actually captured, so
# an intermittent "audio went silent mid-recording" bug is self-diagnosing from
# the debug log alone (no need to re-derive it). Set VOICECLIP_DEBUG_AUDIO=wav to
# ALSO dump each WAV to ~/.voice-clip/debug_recordings/ for offline inspection.
_DEBUG_AUDIO = os.environ.get("VOICECLIP_DEBUG_AUDIO", "").strip().lower()
_DEBUG_AUDIO_ON = _DEBUG_AUDIO not in ("", "0", "false", "no")
_DEBUG_AUDIO_WAV = _DEBUG_AUDIO == "wav"


def _trace_captured_audio(audio_data):
    """Log a per-second RMS bar chart of a captured int16 mono buffer.

    Cheap (numpy over a few seconds of 16kHz) and only runs under
    VOICECLIP_DEBUG_AUDIO. A run of near-zero seconds after real speech is the
    signature of the #277 mid-recording silence."""
    try:
        n = len(audio_data)
        if n == 0:
            _log("DEBUG_AUDIO: captured 0 frames", tag="recorder")
            return
        secs = max(1, int(round(n / SAMPLE_RATE)))
        parts = []
        for s in range(secs):
            seg = audio_data[s * SAMPLE_RATE:(s + 1) * SAMPLE_RATE].astype(np.float32)
            rms = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
            parts.append(f"{s}s:{int(rms)}")
        _log(f"DEBUG_AUDIO: {n/SAMPLE_RATE:.1f}s captured | per-sec RMS "
             + " ".join(parts), tag="recorder")
        if _DEBUG_AUDIO_WAV:
            d = os.path.join(STATE_DIR, "debug_recordings")
            os.makedirs(d, exist_ok=True)
            # monotonic-ish name without Date.now: use frame count + a counter file
            import glob
            idx = len(glob.glob(os.path.join(d, "rec_*.wav")))
            path = os.path.join(d, f"rec_{idx:04d}_{secs}s.wav")
            with wave.open(path, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(audio_data.tobytes())
            _log(f"DEBUG_AUDIO: wrote {path}", tag="recorder")
    except Exception as e:
        _log(f"DEBUG_AUDIO trace failed: {e}", tag="recorder")

# Microphone preference values. "airpods" is the default (Riaan dictates on
# AirPods most of the day); note AirPods record over Bluetooth HFP, which is
# narrowband — transcription quality is measurably worse than the built-in mic
# (see GOTCHAS.md). The menu toggle exists so switching back is one click.
MIC_AIRPODS = "airpods"
MIC_MACBOOK = "macbook"
MIC_SYSTEM = "system"
MIC_PREFERENCES = (MIC_AIRPODS, MIC_MACBOOK, MIC_SYSTEM)
DEFAULT_MIC_PREFERENCE = MIC_AIRPODS


def _reinit_portaudio():
    """Rebuild PortAudio's device table.

    PortAudio snapshots CoreAudio devices at init. Bluetooth devices get a NEW
    CoreAudio ID on every reconnect, so in a long-running process the snapshot
    goes stale and every InputStream open fails with paInternalError (-9986) —
    even for the system default (Jarvis #265). Only a terminate/initialize
    cycle rescans. Safe here because Recorder never holds an open stream when
    it calls this.
    """
    try:
        sd._terminate()
        sd._initialize()
        _log("PortAudio re-initialized (device table refreshed)", tag="recorder")
    except Exception as e:
        _log(f"PortAudio re-init failed: {e}", tag="recorder")


def _find_input_device(devices, needle):
    """Return the index of the first input device whose name contains needle."""
    for i, dev in enumerate(devices):
        if needle.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
            return i
    return None


# The device name each preference actually asks for. A resolution that does
# not land on this needle is a fallback, not a match — see resolve_input_device.
_PREFERENCE_NEEDLE = {
    MIC_AIRPODS: "AirPods",
    MIC_MACBOOK: "MacBook",
}


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


def resolve_with_refresh(preference):
    """resolve_input_device, but retried once against a rebuilt device table
    if the preferred device wasn't found.

    PortAudio snapshots CoreAudio at init and Bluetooth devices get a new ID
    on every reconnect, so connecting AirPods to an already-running daemon
    leaves them invisible — resolution quietly falls back to the MacBook mic
    and STAYS there until restart (#270). The #265 refresh can't catch this:
    it only fires when opening the stream raises, and the fallback opens just
    fine. So detect the fallback itself and refresh before opening.

    Costs a terminate/initialize only on the fallback path; a preference that
    resolves first try (or system default, which asks for nothing) is
    untouched.
    """
    device, name = resolve_input_device(preference)
    needle = _PREFERENCE_NEEDLE.get(preference)
    if needle is None or needle.lower() in name.lower():
        return device, name

    _log(
        f"{preference} wanted but resolved to {name} — refreshing device table",
        tag="recorder",
    )
    _reinit_portaudio()
    device, name = resolve_input_device(preference)
    if needle.lower() not in name.lower():
        # Genuinely not connected — the fallback is correct, just say so once
        # so a silent downgrade is never invisible.
        _log(f"{preference} not present; using {name}", tag="recorder")
    return device, name


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
                device, name = resolve_with_refresh(self.mic_preference)
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
                # First open failed — most likely the device table is stale
                # (Bluetooth reconnect changed the CoreAudio ID, #265), or the
                # device vanished between resolve and open. Refresh PortAudio,
                # re-resolve, and retry; last resort is the system default.
                _log(f"open {name} failed ({e}) — refreshing device table", tag="recorder")
                _reinit_portaudio()
                try:
                    device, name = resolve_input_device(self.mic_preference)
                    if device is None:
                        try:
                            name = sd.query_devices(kind="input")["name"]
                        except Exception:
                            pass
                    self.last_device_name = name
                    self._stream = self._open_stream(device)
                except Exception as e2:
                    if device is None:
                        self._recording = False
                        self._hook("post_close")  # undo pre_open masking
                        raise
                    _log(f"open {name} failed after re-init ({e2}) — trying system default", tag="recorder")
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
        if _DEBUG_AUDIO_ON:
            _trace_captured_audio(audio_data)
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
