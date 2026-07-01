"""Audio recorder using sounddevice — captures mic input to a WAV buffer."""

import io
import wave
import threading
import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # 16kHz — good for speech recognition
CHANNELS = 1
DTYPE = "int16"
BLOCKSIZE = 1024


class Recorder:
    def __init__(self, on_level=None):
        """
        on_level: optional callback(float) called with RMS level 0.0-1.0
                  on each audio block (for VU meter).
        """
        self._frames = []
        self._stream = None
        self._recording = False
        self._on_level = on_level
        self._lock = threading.Lock()

    @property
    def is_recording(self):
        return self._recording

    def start(self):
        """Start recording from default mic."""
        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._recording = True
            # Use the macOS system default input device (System Settings > Sound > Input).
            # Change your mic there — VoiceClip follows whatever you pick.
            # Note: AirPods/Bluetooth use a low-quality 8kHz SCO codec that hurts
            # transcription; prefer a wired/built-in mic for accuracy (see GOTCHAS.md).
            device = None

            self._stream = sd.InputStream(
                device=device,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=BLOCKSIZE,
                callback=self._audio_callback,
            )
            self._stream.start()

    def stop(self) -> bytes:
        """Stop recording and return WAV bytes."""
        with self._lock:
            if not self._recording:
                return b""
            self._recording = False
            self._stream.stop()
            self._stream.close()
            self._stream = None
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
