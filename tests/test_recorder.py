"""Tests for recorder module — WAV generation and audio math, no hardware needed."""

import io
import struct
import wave

import numpy as np
import pytest

from recorder import SAMPLE_RATE, CHANNELS, DTYPE, BLOCKSIZE


class TestRecorderConstants:
    """Verify audio configuration is valid for speech recognition."""

    def test_sample_rate_16khz(self):
        """16kHz is standard for speech recognition models."""
        assert SAMPLE_RATE == 16000

    def test_mono_channel(self):
        """Speech recognition works best with mono audio."""
        assert CHANNELS == 1

    def test_dtype_int16(self):
        """int16 is the standard WAV sample format."""
        assert DTYPE == "int16"

    def test_blocksize_power_of_two(self):
        """Block size should be a power of 2 for efficient FFT/processing."""
        assert BLOCKSIZE > 0
        assert (BLOCKSIZE & (BLOCKSIZE - 1)) == 0


class TestWavGeneration:
    """Test WAV byte generation from numpy frames."""

    @staticmethod
    def frames_to_wav(frames: list, sample_rate=SAMPLE_RATE, channels=CHANNELS) -> bytes:
        """Reproduce the WAV conversion logic from Recorder.stop()."""
        audio_data = np.concatenate(frames, axis=0) if frames else np.array([], dtype="int16")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data.tobytes())
        return buf.getvalue()

    def test_empty_frames_produce_valid_wav(self):
        """Empty frame list should produce a valid (header-only) WAV."""
        wav_bytes = self.frames_to_wav([])
        assert wav_bytes[:4] == b"RIFF"
        assert wav_bytes[8:12] == b"WAVE"

    def test_single_frame_wav(self):
        """A single frame should produce a valid WAV with correct properties."""
        frame = np.zeros((BLOCKSIZE, 1), dtype="int16")
        wav_bytes = self.frames_to_wav([frame])

        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            assert wf.getnchannels() == CHANNELS
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == SAMPLE_RATE
            assert wf.getnframes() == BLOCKSIZE

    def test_multiple_frames_concatenated(self):
        """Multiple frames should be concatenated in order."""
        n_frames = 5
        frames = [np.ones((BLOCKSIZE, 1), dtype="int16") * i for i in range(n_frames)]
        wav_bytes = self.frames_to_wav(frames)

        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            assert wf.getnframes() == BLOCKSIZE * n_frames

    def test_wav_roundtrip(self):
        """WAV bytes should decode back to the original audio data."""
        original = np.array([[100], [-200], [32767], [-32768]], dtype="int16")
        wav_bytes = self.frames_to_wav([original])

        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        decoded = np.frombuffer(raw, dtype="int16")
        np.testing.assert_array_equal(decoded, original.flatten())


class TestRmsCalculation:
    """Test the RMS level calculation used for the VU meter."""

    @staticmethod
    def compute_level(indata: np.ndarray) -> float:
        """Reproduce the RMS calculation from Recorder._audio_callback."""
        rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
        return min(rms / 1500.0, 1.0)

    def test_silence_gives_zero(self):
        """Silence (all zeros) should produce level 0."""
        silence = np.zeros((BLOCKSIZE, 1), dtype="int16")
        assert self.compute_level(silence) == 0.0

    def test_max_amplitude_clips_to_one(self):
        """Full-scale int16 should clip to 1.0."""
        loud = np.full((BLOCKSIZE, 1), 32767, dtype="int16")
        assert self.compute_level(loud) == 1.0

    def test_moderate_speech_in_range(self):
        """Typical speech RMS (~500) should be in the 0.1-0.7 range."""
        speech = np.full((BLOCKSIZE, 1), 500, dtype="int16")
        level = self.compute_level(speech)
        assert 0.1 < level < 0.7

    def test_level_monotonic(self):
        """Louder audio should produce a higher level."""
        quiet = np.full((BLOCKSIZE, 1), 100, dtype="int16")
        loud = np.full((BLOCKSIZE, 1), 1000, dtype="int16")
        assert self.compute_level(quiet) < self.compute_level(loud)
