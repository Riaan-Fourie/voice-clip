"""
py2app setup for VoiceClip.app

Build with:
    python setup_app.py py2app

The resulting .app bundle will be in dist/VoiceClip.app
"""

from setuptools import setup

APP = ["main.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "iconfile": None,  # TODO: add custom icon
    "plist": {
        "CFBundleName": "VoiceClip",
        "CFBundleIdentifier": "com.riaanfourie.voiceclip",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,  # Hide from Dock (menubar-only app)
        "NSMicrophoneUsageDescription": "VoiceClip needs microphone access to record your voice for speech-to-text.",
        "LSMinimumSystemVersion": "13.0",
    },
    "includes": [
        "rumps",
        "sounddevice",
        "numpy",
        "mlx_whisper",
        "mlx",
        "objc",
        "AppKit",
        "Foundation",
        "Quartz",
        "PyObjCTools",
    ],
    "packages": [
        "mlx",
        "mlx_whisper",
        "huggingface_hub",
        "tokenizers",
        "safetensors",
        "tqdm",
        "regex",
    ],
    "frameworks": [],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
