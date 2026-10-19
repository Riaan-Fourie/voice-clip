"""Keep the test suite out of the live app's state (Jarvis #767).

utils resolves ~/.voice-clip when it is imported, and main.py points its logging
at that folder, so a test run used to write fake "recording via ..." lines into
the live debug log, where they read like real dictations. HOME is swapped here,
before any test module imports a project module."""
import os
import tempfile

_HOME = tempfile.mkdtemp(prefix="voiceclip-test-home-")
os.environ["HOME"] = _HOME

import utils  # noqa: E402

assert utils.STATE_DIR.startswith(_HOME), utils.STATE_DIR
