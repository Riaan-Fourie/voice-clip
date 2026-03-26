"""Smoke tests — verify that all modules import without errors on macOS."""

import sys
import pytest

# These tests only run on macOS since the modules depend on AppKit/Quartz
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


def test_import_utils():
    import utils
    assert hasattr(utils, "_log")
    assert hasattr(utils, "STATE_DIR")
    assert hasattr(utils, "LOG_PATH")


def test_import_recorder():
    import recorder
    assert hasattr(recorder, "Recorder")


def test_import_transcriber():
    import transcriber
    assert hasattr(transcriber, "Transcriber")


def test_import_hotkey():
    import hotkey
    assert hasattr(hotkey, "HotkeyListener")


def test_import_overlay():
    import overlay
    assert hasattr(overlay, "RecordingOverlay")


def test_import_main():
    import main
    assert hasattr(main, "VoiceClipApp")
