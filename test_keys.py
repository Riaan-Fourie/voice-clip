#!/usr/bin/env python3
"""Minimal test: log ALL modifier key events to verify global monitoring works."""

import sys
from AppKit import NSApplication, NSEvent
from PyObjCTools import AppHelper

NSFlagsChangedMask = 1 << 12

def handler(event):
    keycode = event.keyCode()
    flags = event.modifierFlags()
    print(f"EVENT: keyCode={keycode} flags=0x{flags:08x}", flush=True)

print("Listening for modifier key events (Shift, Cmd, Option, Fn, Ctrl)...", flush=True)
print("Press any modifier key. Press Ctrl+C to quit.", flush=True)

app = NSApplication.sharedApplication()
monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
    NSFlagsChangedMask, handler
)
print(f"Monitor: {monitor}", flush=True)

AppHelper.runConsoleEventLoop()
