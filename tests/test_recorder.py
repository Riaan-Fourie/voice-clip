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


class TestMicPreference:
    """Test mic preference → device resolution (pure logic, no hardware)."""

    DEVICES = [
        {"name": "Riaan's AirPods Pro", "max_input_channels": 1},
        {"name": "MacBook Air Microphone", "max_input_channels": 1},
        {"name": "MacBook Air Speakers", "max_input_channels": 0},
        {"name": "BlackHole 2ch", "max_input_channels": 2},
    ]

    def test_default_preference_is_airpods(self):
        from recorder import DEFAULT_MIC_PREFERENCE, MIC_AIRPODS
        assert DEFAULT_MIC_PREFERENCE == MIC_AIRPODS

    def test_airpods_selected_when_connected(self):
        from recorder import resolve_input_device, MIC_AIRPODS
        idx, name = resolve_input_device(MIC_AIRPODS, self.DEVICES)
        assert idx == 0
        assert "AirPods" in name

    def test_airpods_falls_back_to_macbook(self):
        from recorder import resolve_input_device, MIC_AIRPODS
        devices = [d for d in self.DEVICES if "AirPods" not in d["name"]]
        idx, name = resolve_input_device(MIC_AIRPODS, devices)
        assert "MacBook Air Microphone" == devices[idx]["name"]

    def test_airpods_falls_back_to_system_default(self):
        from recorder import resolve_input_device, MIC_AIRPODS
        devices = [{"name": "BlackHole 2ch", "max_input_channels": 2}]
        idx, name = resolve_input_device(MIC_AIRPODS, devices)
        assert idx is None
        assert name == "System Default"

    def test_macbook_preference_skips_airpods(self):
        from recorder import resolve_input_device, MIC_MACBOOK
        idx, name = resolve_input_device(MIC_MACBOOK, self.DEVICES)
        assert idx == 1

    def test_output_only_device_never_selected(self):
        """Speakers (0 input channels) must never match, even by name."""
        from recorder import resolve_input_device, MIC_MACBOOK
        devices = [{"name": "MacBook Air Speakers", "max_input_channels": 0}]
        idx, name = resolve_input_device(MIC_MACBOOK, devices)
        assert idx is None

    def test_system_preference_uses_default(self):
        from recorder import resolve_input_device, MIC_SYSTEM
        idx, name = resolve_input_device(MIC_SYSTEM, self.DEVICES)
        assert idx is None
        assert name == "System Default"

    def test_case_insensitive_match(self):
        from recorder import resolve_input_device, MIC_AIRPODS
        devices = [{"name": "riaan's airpods pro", "max_input_channels": 1}]
        idx, _ = resolve_input_device(MIC_AIRPODS, devices)
        assert idx == 0
