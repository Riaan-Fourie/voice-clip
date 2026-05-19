"""Speech-to-text using mlx-whisper (local Whisper on Apple Silicon via Metal GPU)."""

import io
import os
import threading
import wave
from datetime import datetime

import mlx.core as mx
import mlx_whisper
import numpy as np

from utils import _log as _log_base, STATE_DIR

FAILED_DIR = os.path.join(STATE_DIR, "failed")
PROPER_NOUNS_PATH = os.path.join(STATE_DIR, "proper_nouns.txt")

# whisper-small.en-mlx: small + English-only, fastest viable for live dictation
# on Apple Silicon. Swap to "mlx-community/whisper-large-v3-turbo" if you want
# stronger non-English-name accuracy at the cost of 2-3x latency.
MODEL = "mlx-community/whisper-small.en-mlx"

DEFAULT_PROPER_NOUNS = """# VoiceClip proper nouns — biases Whisper toward these tokens.
# One entry per line. Add names, jargon, abbreviations Whisper keeps mishearing.
# Comments (#) and blank lines are ignored. Whisper truncates at ~224 tokens.

Dewald
Riaan
Fourie
HashDirectors
SumSub
Hetzner
Havoc
Kennet
FBAR
Cayman
Supabase
Vercel
Monday.com
Sølūnâ
Affinity
Inven
Telegram
Matrix
Beeper
mlx
Whisper
"""


def _log(msg):
    _log_base(msg, tag="transcriber")


def _load_proper_nouns() -> str:
    """Read the user-editable proper nouns file. Create it with defaults if missing.

    Returns a comma-joined string suitable for Whisper's `initial_prompt` kwarg.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(PROPER_NOUNS_PATH):
        with open(PROPER_NOUNS_PATH, "w") as f:
            f.write(DEFAULT_PROPER_NOUNS)
        _log(f"Created default proper nouns file at {PROPER_NOUNS_PATH}")
    try:
        with open(PROPER_NOUNS_PATH) as f:
            words = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
        return ", ".join(words)
    except Exception as e:
        _log(f"Failed to read proper nouns file: {e}")
        return ""


def _wav_bytes_to_float32(wav_bytes: bytes) -> np.ndarray:
    """Decode 16-bit mono WAV bytes into a float32 numpy array in [-1, 1].

    Bypasses the disk round-trip mlx-whisper would otherwise force.
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class Transcriber:
    def __init__(self):
        self._model_loaded = False
        self._init_lock = threading.Lock()
        self._initial_prompt = _load_proper_nouns()
        if self._initial_prompt:
            count = len([w for w in self._initial_prompt.split(",") if w.strip()])
            _log(f"Loaded {count} proper nouns for initial_prompt")

    def _ensure_init(self):
        """Lazy init — download/load model on first use by transcribing a short silence."""
        with self._init_lock:
            if self._model_loaded:
                return
            try:
                _log(f"Loading Whisper model: {MODEL}")
                silence = np.zeros(1600, dtype=np.float32)  # 0.1s @ 16kHz
                mlx_whisper.transcribe(silence, path_or_hf_repo=MODEL)
                mx.clear_cache()
                self._model_loaded = True
                _log("Whisper model loaded successfully")
            except Exception as e:
                _log(f"Failed to load Whisper model: {e}")

    def reload_proper_nouns(self):
        """Re-read the proper nouns file. Call after editing it at runtime."""
        self._initial_prompt = _load_proper_nouns()

    def transcribe(self, wav_bytes: bytes, callback=None):
        """Transcribe WAV bytes using mlx-whisper.

        callback(text, error) is called when done.
        If callback is None, blocks and returns (text, error).
        """
        self._ensure_init()

        if callback:
            threading.Thread(
                target=self._do_transcribe,
                args=(wav_bytes, callback),
                daemon=True,
            ).start()
        else:
            return self._do_transcribe_sync(wav_bytes)

    def _do_transcribe_sync(self, wav_bytes):
        """Synchronous transcription. Audio is decoded in memory; no disk round-trip."""
        try:
            audio = _wav_bytes_to_float32(wav_bytes)
            _log(f"Transcribing {len(audio) / 16000:.1f}s of audio")
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=MODEL,
                initial_prompt=self._initial_prompt or None,
            )
            text = result.get("text", "").strip()
            _log(f"Transcription result: {text[:80]}")

            if text:
                return text, None
            else:
                self._save_failed(wav_bytes, "No speech detected")
                return None, "No speech detected"
        except Exception as e:
            _log(f"Transcription error: {e}")
            error_msg = str(e)
            self._save_failed(wav_bytes, error_msg)
            return None, error_msg
        finally:
            # Release MLX workspace buffers (KV cache, activations).
            # Weights stay warm — only transient compute memory is freed.
            mx.clear_cache()

    def _do_transcribe(self, wav_bytes, callback):
        """Async transcription wrapper."""
        text, error = self._do_transcribe_sync(wav_bytes)
        callback(text, error)

    def _save_failed(self, wav_bytes: bytes, error_msg: str):
        """Save failed recording for retry."""
        os.makedirs(FAILED_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_path = os.path.join(FAILED_DIR, f"{timestamp}.wav")
        err_path = os.path.join(FAILED_DIR, f"{timestamp}.error")
        with open(wav_path, "wb") as f:
            f.write(wav_bytes)
        with open(err_path, "w") as f:
            f.write(error_msg)

    def retry_failed(self, callback=None):
        """Retry all failed transcriptions. Returns list of (filename, text, error)."""
        if not os.path.exists(FAILED_DIR):
            return []

        results = []
        for fname in sorted(os.listdir(FAILED_DIR)):
            if not fname.endswith(".wav"):
                continue
            wav_path = os.path.join(FAILED_DIR, fname)
            err_path = wav_path.replace(".wav", ".error")

            with open(wav_path, "rb") as f:
                wav_bytes = f.read()

            text, error = self.transcribe(wav_bytes)
            if text and not error:
                os.unlink(wav_path)
                if os.path.exists(err_path):
                    os.unlink(err_path)
                results.append((fname, text, None))
                if callback:
                    callback(fname, text, None)
            else:
                results.append((fname, None, error))
                if callback:
                    callback(fname, None, error)

        return results

    def get_failed_count(self) -> int:
        """Return number of failed recordings waiting for retry."""
        if not os.path.exists(FAILED_DIR):
            return 0
        return len([f for f in os.listdir(FAILED_DIR) if f.endswith(".wav")])
