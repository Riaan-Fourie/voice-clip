"""Tests for the shared utils module."""

import os
import tempfile
from unittest.mock import patch

import pytest


def test_log_creates_state_dir_and_writes(tmp_path):
    """_log() should create the state directory and append a timestamped line."""
    log_path = tmp_path / "voiceclip-debug.log"

    with patch("utils.STATE_DIR", str(tmp_path)), \
         patch("utils.LOG_PATH", str(log_path)):
        from utils import _log
        _log("hello world")
        _log("second line", tag="test")

    contents = log_path.read_text()
    lines = contents.strip().split("\n")
    assert len(lines) == 2
    assert "hello world" in lines[0]
    assert "[test]" in lines[1]
    assert "second line" in lines[1]


def test_log_with_tag(tmp_path):
    """_log() with a tag should include [tag] prefix."""
    log_path = tmp_path / "voiceclip-debug.log"

    with patch("utils.STATE_DIR", str(tmp_path)), \
         patch("utils.LOG_PATH", str(log_path)):
        from utils import _log
        _log("tagged message", tag="hotkey")

    contents = log_path.read_text()
    assert "[hotkey]" in contents
    assert "tagged message" in contents


def test_log_without_tag(tmp_path):
    """_log() without a tag should not include bracket prefix."""
    log_path = tmp_path / "voiceclip-debug.log"

    with patch("utils.STATE_DIR", str(tmp_path)), \
         patch("utils.LOG_PATH", str(log_path)):
        from utils import _log
        _log("plain message")

    contents = log_path.read_text()
    assert "plain message" in contents
    # Should not have any [tag] prefix
    assert "] plain" not in contents or "[]" not in contents
