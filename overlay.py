"""Floating VU meter overlay — shows live audio level bars while recording."""

import threading
import time

import objc
from AppKit import (
    NSPanel,
    NSView,
    NSColor,
    NSBezierPath,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskUtilityWindow,
    NSBackingStoreBuffered,
    NSScreen,
    NSApplication,
    NSRect,
    NSPoint,
    NSSize,
    NSGraphicsContext,
)
from Foundation import NSTimer, NSRunLoop, NSDefaultRunLoopMode

# kCGMaximumWindowLevelKey — highest possible, renders above everything
import Quartz
ABOVE_ALL_LEVEL = Quartz.kCGScreenSaverWindowLevel + 100


NUM_BARS = 8
BAR_WIDTH = 8
BAR_GAP = 4
BAR_MAX_HEIGHT = 50
WINDOW_PADDING = 16
WINDOW_WIDTH = NUM_BARS * (BAR_WIDTH + BAR_GAP) - BAR_GAP + WINDOW_PADDING * 2
WINDOW_HEIGHT = BAR_MAX_HEIGHT + WINDOW_PADDING * 2
CORNER_RADIUS = 12


class VUMeterView(NSView):
    """Custom view that draws animated audio level bars."""

    def initWithFrame_(self, frame):
        self = objc.super(VUMeterView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._levels = [0.0] * NUM_BARS
        self._target_level = 0.0
        return self

    def setLevel_(self, level):
        """Set the current audio level (0.0 - 1.0)."""
        self._target_level = float(level)

    def updateBars(self):
        """Animate bars toward target level with some variation."""
        import random
        for i in range(NUM_BARS):
            # Each bar gets a slightly different target for organic look
            variation = random.uniform(0.5, 1.3)
            target = min(self._target_level * variation, 1.0)
            # Smooth interpolation
            self._levels[i] += (target - self._levels[i]) * 0.4
            self._levels[i] = max(0.02, self._levels[i])  # minimum visible height
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        # Draw rounded background
        bg_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), CORNER_RADIUS, CORNER_RADIUS
        )
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.12, 0.9).set()
        bg_path.fill()

        # Draw bars
        for i in range(NUM_BARS):
            bar_height = max(3, self._levels[i] * BAR_MAX_HEIGHT)
            x = WINDOW_PADDING + i * (BAR_WIDTH + BAR_GAP)
            y = WINDOW_PADDING + (BAR_MAX_HEIGHT - bar_height) / 2  # center vertically

            bar_rect = NSRect(
                NSPoint(x, y),
                NSSize(BAR_WIDTH, bar_height),
            )
            bar_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bar_rect, BAR_WIDTH / 2, BAR_WIDTH / 2
            )

            # Color gradient: green -> yellow -> red based on level
            level = self._levels[i]
            if level < 0.5:
                r, g, b = 0.2, 0.8, 0.4  # green
            elif level < 0.75:
                r, g, b = 0.9, 0.8, 0.2  # yellow
            else:
                r, g, b = 0.9, 0.3, 0.2  # red

            NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 0.95).set()
            bar_path.fill()

    def isFlipped(self):
        return False


class RecordingOverlay:
    """Manages the floating VU meter window."""

    def __init__(self):
        self._window = None
        self._view = None
        self._timer = None
        self._visible = False

    def _ensure_window(self):
        """Create the window if it doesn't exist."""
        if self._window is not None:
            return

        # Position at bottom center of main screen
        screen = NSScreen.mainScreen()
        screen_frame = screen.frame()
        x = (screen_frame.size.width - WINDOW_WIDTH) / 2
        y = 80  # 80px from bottom

        frame = NSRect(
            NSPoint(x, y),
            NSSize(WINDOW_WIDTH, WINDOW_HEIGHT),
        )

        # Use NSPanel with NonactivatingPanel — can float above fullscreen apps
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel | NSWindowStyleMaskUtilityWindow
        self._window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            style,
            NSBackingStoreBuffered,
            False,
        )
        self._window.setLevel_(ABOVE_ALL_LEVEL)
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setIgnoresMouseEvents_(True)
        self._window.setHidesOnDeactivate_(False)
        self._window.setFloatingPanel_(True)
        # canJoinAllSpaces | fullScreenAuxiliary | stationary
        self._window.setCollectionBehavior_((1 << 0) | (1 << 8) | (1 << 4))

        self._view = VUMeterView.alloc().initWithFrame_(
            NSRect(NSPoint(0, 0), NSSize(WINDOW_WIDTH, WINDOW_HEIGHT))
        )
        self._window.setContentView_(self._view)

    def show(self):
        """Show the overlay window and start animation timer."""
        if self._visible:
            return

        def _show_on_main():
            self._ensure_window()
            self._window.orderFrontRegardless()
            self._visible = True
            # Start animation timer (30fps)
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / 30.0, self._view, objc.selector(VUMeterView.updateBars, signature=b"v@:"),
                None, True,
            )

        # Must run on main thread
        from AppKit import NSApp
        if threading.current_thread() is threading.main_thread():
            _show_on_main()
        else:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_show_on_main)

    def hide(self):
        """Hide the overlay window."""
        if not self._visible:
            return

        def _hide_on_main():
            if self._timer:
                self._timer.invalidate()
                self._timer = None
            if self._window:
                self._window.orderOut_(None)
            self._visible = False

        if threading.current_thread() is threading.main_thread():
            _hide_on_main()
        else:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_hide_on_main)

    def set_level(self, level: float):
        """Update the audio level (called from recorder callback)."""
        if self._view:
            self._view.setLevel_(level)

    @property
    def visible(self):
        return self._visible
