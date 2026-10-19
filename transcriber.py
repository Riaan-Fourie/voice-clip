"""Speech-to-text using Parakeet (parakeet-mlx, local on Apple Silicon via Metal GPU)."""

import io
import os
import threading
import time
import wave
from datetime import datetime

import mlx.core as mx
import mlx.nn as nn
import numpy as np

from names import correct_names, load_replacements
from utils import _log as _log_base, STATE_DIR

FAILED_DIR = os.path.join(STATE_DIR, "failed")
PROPER_NOUNS_PATH = os.path.join(STATE_DIR, "proper_nouns.txt")

# Parakeet TDT 0.6B v2, English-only (Jarvis #769). Every active Mac dictation
# app moved to Parakeet in 2025-2026, and on Riaan's own clips it held up far
# better than whisper-small.en on the built-in mic in a noisy room, at about
# 0.2 s a clip (research: docs/research/voiceclip-stt-engines-2026-10-19 in
# Jarvis). v2 is English-only; v3 adds 24 more languages for a slightly
# larger model. Parakeet takes no prompt, so proper nouns are fixed after
# transcription (names.py) instead of through Whisper's initial_prompt.
MODEL = "mlx-community/parakeet-tdt-0.6b-v2"
MIN_SAMPLES = 1600  # 0.1 s at 16 kHz; shorter clips cannot hold a word

# Quantised to 8 bits at load (Riaan's call, 2026-10-19): the weights drop from
# ~1.22 GB (bf16) to ~0.74 GB, and the settled footprint from ~1.49 to ~0.92 GB.
# On 12 of his clips the text matched the full model on 8 and differed by a
# word or two on the rest, with neither version right where they differed.
# 4-bit (~0.49 GB) changed the wording on 10 of 12, so it was not taken.
QUANT_BITS = 8
QUANT_GROUP_SIZE = 64

# The weights are ~0.74 GB of GPU memory at 8 bits. On this 16 GB Mac, which runs
# deep in swap, macOS paged them out whenever dictation sat idle for a few
# minutes, and the next dictation took up to 6 s to fault them back in
# (witnessed on #769; whisper-small.en did the same, 4.3 s after 10 idle
# minutes). MLX's wired limit keeps buffers resident: set before the model
# loads, it pins the weights (measured: +1.06 GB wired against +0.4 GB
# without, on the bf16 model; +765 MB on the 8-bit one). The headroom above
# the weights covers the warm-up's working buffers.
WIRED_LIMIT_BYTES = 1_000_000_000

# The pin alone was not enough: after an hour's pause the first dictation
# still took 5.7 s while the next ones took 0.4 s (2026-10-19, swap 15.8 of
# 16.4 GB used). macOS also evicts the Python and MLX code around the model.
# So while dictation is idle, run the model on half a second of silence once
# a minute. Measured on #769: under a minute idle, a dictation took ~0.4 s;
# after 3+ minutes, 1.4 to 5.9 s. The pass costs ~0.1 s of GPU a minute.
KEEP_WARM_SECONDS = 60

DEFAULT_PROPER_NOUNS = """# VoiceClip proper nouns — restored after transcription (names.py).
# One entry per line. Names with internal capitals, digits or accents
# (HashDirectors, SumSub, FBAR) are rebuilt when heard split or in lower case.
# For a name the engine mishears outright, add a line to replacements.txt.
# Comments (#) and blank lines are ignored.

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
NDA
Wilsch
Marius
"""


def _log(msg):
    _log_base(msg, tag="transcriber")


def _load_proper_nouns() -> list:
    """Read the user-editable proper nouns file. Create it with defaults if missing."""
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
        return words
    except Exception as e:
        _log(f"Failed to read proper nouns file: {e}")
        return []


def _wav_bytes_to_float32(wav_bytes: bytes) -> np.ndarray:
    """Decode mono WAV bytes into a float32 numpy array in [-1, 1].

    Fast path: stdlib `wave` for 16-bit PCM (what our Recorder emits). Bypasses
    the disk and ffmpeg round-trip parakeet-mlx's file loader would force.

    Fallback: libsndfile via `soundfile` for any encoding `wave` rejects — notably
    a format-3 IEEE-float WAV, which raises `wave.Error: unknown format: 3`. That
    exception used to bubble up and (via the un-guarded UI thread) wedge the whole
    app (issue #170); decoding it instead keeps dictation alive.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        import soundfile as sf  # lazy: only non-PCM inputs pay the import cost
        data, _sr = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=False)
        if data.ndim > 1:  # mix down any stray multi-channel input to mono
            data = data.mean(axis=1)
        return np.ascontiguousarray(data, dtype=np.float32)


def _pin_model_memory():
    """Keep the model resident between dictations (see WIRED_LIMIT_BYTES).
    Best-effort: without it dictation still works, only slower after idle."""
    try:
        cap = mx.device_info().get("max_recommended_working_set_size", 0)
        limit = min(WIRED_LIMIT_BYTES, int(cap * 0.5)) if cap else WIRED_LIMIT_BYTES
        mx.set_wired_limit(limit)
        _log(f"Wired limit set to {limit / 1e9:.2f} GB")
    except Exception as e:
        _log(f"Could not set the wired limit: {e}")


class Transcriber:
    def __init__(self):
        self._model = None
        self._init_lock = threading.Lock()
        self._run_lock = threading.Lock()  # one model pass at a time
        self._last_used = time.monotonic()
        self._warm_thread = None
        self._warm_passes = 0
        self._names = _load_proper_nouns()
        self._replacements = load_replacements()
        _log(f"Loaded {len(self._names)} proper nouns, {len(self._replacements)} replacements")

    @property
    def _model_loaded(self):
        return self._model is not None

    def _ensure_init(self):
        """Lazy init — download/load the model on first use and warm it up."""
        with self._init_lock:
            if self._model is not None:
                return
            try:
                _pin_model_memory()
                from parakeet_mlx import from_pretrained
                _log(f"Loading speech model: {MODEL}")
                model = from_pretrained(MODEL)
                nn.quantize(model, group_size=QUANT_GROUP_SIZE, bits=QUANT_BITS)
                mx.clear_cache()  # release the bf16 weights the quantised ones replace
                self._run(model, np.zeros(16000, dtype=np.float32))  # 1 s warm-up
                self._model = model
                _log("Speech model loaded successfully")
                self._start_keep_warm()
            except Exception as e:
                _log(f"Failed to load speech model: {e}")
            finally:
                mx.clear_cache()

    @staticmethod
    def _run(model, audio):
        """One Parakeet pass over float32 16 kHz mono audio, all in memory."""
        from parakeet_mlx.audio import get_logmel
        mel = get_logmel(mx.array(audio), model.preprocessor_config)
        return model.generate(mel)[0].text.strip()

    def _start_keep_warm(self):
        if self._warm_thread is None:
            self._warm_thread = threading.Thread(target=self._keep_warm_loop, daemon=True)
            self._warm_thread.start()

    def _keep_warm_loop(self):
        while True:
            time.sleep(KEEP_WARM_SECONDS)
            self._keep_warm_once()

    def _keep_warm_once(self):
        """Touch the model if dictation has been idle a full interval.

        Never waits for a real transcription: if one holds the model, this
        pass is skipped. Returns True when it ran."""
        if self._model is None:
            return False
        if time.monotonic() - self._last_used < KEEP_WARM_SECONDS:
            return False
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            self._run(self._model, np.zeros(8000, dtype=np.float32))
            self._warm_passes += 1
            if self._warm_passes == 1:  # one line, not one a minute
                _log(f"keep-warm active: model touched every {KEEP_WARM_SECONDS}s while idle")
            return True
        except Exception as e:
            _log(f"keep-warm pass failed: {e}")
            return False
        finally:
            self._last_used = time.monotonic()
            self._run_lock.release()
            mx.clear_cache()

    def reload_proper_nouns(self):
        """Re-read the proper nouns and replacements files. Call after editing them."""
        self._names = _load_proper_nouns()
        self._replacements = load_replacements()

    def transcribe(self, wav_bytes: bytes, callback=None):
        """Transcribe WAV bytes with Parakeet.

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
            if self._model is None:
                raise RuntimeError("speech model not loaded")
            audio = _wav_bytes_to_float32(wav_bytes)
            _log(f"Transcribing {len(audio) / 16000:.1f}s of audio")
            text = ""
            if len(audio) >= MIN_SAMPLES:
                with self._run_lock:
                    text = self._run(self._model, audio)
                    self._last_used = time.monotonic()
            if text:
                fixed = correct_names(text, self._names, self._replacements)
                if fixed != text:
                    _log(f"Names corrected: {text[:300]!r} -> {fixed[:300]!r}")
                text = fixed
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
            # Release MLX workspace buffers. Weights stay warm; only transient
            # compute memory is freed (parakeet-mlx grows without this, #34).
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
