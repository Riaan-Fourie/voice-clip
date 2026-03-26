"""Speech-to-text using mlx-whisper (local Whisper on Apple Silicon via Metal GPU)."""

import os
import tempfile
import threading

import mlx_whisper

from utils import _log as _log_base, STATE_DIR

FAILED_DIR = os.path.join(STATE_DIR, "failed")

# Use "base" model — good balance of speed and accuracy (~150MB)
# "tiny" is faster but less accurate (~75MB)
MODEL = "mlx-community/whisper-base-mlx"


def _log(msg):
    _log_base(msg, tag="transcriber")


class Transcriber:
    def __init__(self):
        self._model_loaded = False
        self._init_lock = threading.Lock()

    def _ensure_init(self):
        """Lazy init — download/load model on first use."""
        with self._init_lock:
            if self._model_loaded:
                return
            try:
                _log(f"Loading Whisper model: {MODEL}")
                # Warm up the model by transcribing silence
                # This triggers the model download if needed
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                # Write minimal valid WAV header (silence)
                import struct
                sr = 16000
                duration = 0.1
                n_samples = int(sr * duration)
                data_size = n_samples * 2
                tmp.write(b'RIFF')
                tmp.write(struct.pack('<I', 36 + data_size))
                tmp.write(b'WAVEfmt ')
                tmp.write(struct.pack('<IHHIIHH', 16, 1, 1, sr, sr * 2, 2, 16))
                tmp.write(b'data')
                tmp.write(struct.pack('<I', data_size))
                tmp.write(b'\x00' * data_size)
                tmp.close()

                mlx_whisper.transcribe(tmp.name, path_or_hf_repo=MODEL)
                os.unlink(tmp.name)
                self._model_loaded = True
                _log("Whisper model loaded successfully")
            except Exception as e:
                _log(f"Failed to load Whisper model: {e}")
                try:
                    os.unlink(tmp.name)
                except:
                    pass

    def transcribe(self, wav_bytes: bytes, callback=None):
        """
        Transcribe WAV bytes using mlx-whisper.
        callback(text, error) is called when done.
        If callback is None, blocks and returns (text, error).
        """
        self._ensure_init()

        # Write wav to temp file
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(wav_bytes)
        tmp.close()

        if callback:
            threading.Thread(
                target=self._do_transcribe,
                args=(tmp.name, wav_bytes, callback),
                daemon=True,
            ).start()
        else:
            return self._do_transcribe_sync(tmp.name, wav_bytes)

    def _do_transcribe_sync(self, filepath, wav_bytes):
        """Synchronous transcription."""
        try:
            _log(f"Transcribing {filepath}")
            result = mlx_whisper.transcribe(filepath, path_or_hf_repo=MODEL)
            text = result.get("text", "").strip()
            _log(f"Transcription result: {text[:80]}")

            os.unlink(filepath)

            if text:
                return text, None
            else:
                self._save_failed(wav_bytes, "No speech detected")
                return None, "No speech detected"
        except Exception as e:
            _log(f"Transcription error: {e}")
            try:
                os.unlink(filepath)
            except OSError:
                pass
            error_msg = str(e)
            self._save_failed(wav_bytes, error_msg)
            return None, error_msg

    def _do_transcribe(self, filepath, wav_bytes, callback):
        """Async transcription wrapper."""
        text, error = self._do_transcribe_sync(filepath, wav_bytes)
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
