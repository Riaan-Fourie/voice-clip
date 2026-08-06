"""Floating recording overlay — an animated mascot (mascot.gif) whose cheeks
throw voice-reactive lightning while recording.

The mascot GIF loops on its own; the lightning is drawn live in Cocoa
(NSBezierPath) and scales with the mic level the recorder streams in — a lazy
spark or two when quiet, a full crackling storm when loud. Cheek anchor points
per frame are baked offline into assets/mascot_cheeks.json (so the app needs no
image analysis at runtime). Falls back to a simple pulsing dot if the assets are
missing, so a bad asset never leaves recording with no visible indicator.
"""

import json
import math
import os
import random
import threading

from utils import _log as _log_base


def _log(msg):
    _log_base(msg, tag="overlay")


import objc
from AppKit import (
    NSPanel,
    NSView,
    NSColor,
    NSBezierPath,
    NSImage,
    NSCompositingOperationSourceOver,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskUtilityWindow,
    NSBackingStoreBuffered,
    NSScreen,
    NSRect,
    NSPoint,
    NSSize,
)
from Foundation import NSTimer, NSURL
import Quartz

ABOVE_ALL_LEVEL = Quartz.kCGScreenSaverWindowLevel + 100

# Overlay geometry. The window now covers the WHOLE screen (transparent,
# click-through) so the cheek lightning can grow to fill it the longer you speak.
# The mascot itself stays this size, pinned near the bottom-centre.
MASCOT_SIZE = 118
MASCOT_BOTTOM_MARGIN = 110   # mascot centre this far up from the screen bottom

# Duration ramp: the bolts' reach grows from "just around the body" to "across the
# whole screen" over this many seconds of a single continuous recording. The longer
# you talk, the more the storm consumes the screen.
FILL_SECONDS = 16.0

# How long after show() to ask the WindowServer whether the panel really made it
# on screen. Long enough that compositing has settled (a check inline after
# orderFrontRegardless always reads False), short enough that a rescue still
# lands inside a normal press-and-hold recording (#323).
_ONSCREEN_CHECK_DELAY = 0.35

_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_GIF_PATH = os.path.join(_ASSET_DIR, "mascot.gif")
_CHEEKS_PATH = os.path.join(_ASSET_DIR, "mascot_cheeks.json")


_MASCOT_CACHE = None


def _load_mascot():
    """Load the GIF frames (as NSImage) + baked per-frame delays and cheek
    anchors. Returns (frames, delays, cheeks, frame_size) or None on any failure.

    Cached process-wide: the window (and with it the view) is now rebuilt when it
    wedges (#323), and a rebuild must not cost 20 GIF decodes off disk mid-recording."""
    global _MASCOT_CACHE
    if _MASCOT_CACHE is not None:
        return _MASCOT_CACHE
    try:
        with open(_CHEEKS_PATH) as f:
            meta = json.load(f)
        fw, fh = meta["frame_size"]
        url = NSURL.fileURLWithPath_(_GIF_PATH)
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if src is None:
            _log(f"mascot: could not open {_GIF_PATH}")
            return None
        count = Quartz.CGImageSourceGetCount(src)
        frames, delays, cheeks = [], [], []
        mframes = meta["frames"]
        for i in range(min(count, len(mframes))):
            cg = Quartz.CGImageSourceCreateImageAtIndex(src, i, None)
            img = NSImage.alloc().initWithCGImage_size_(cg, NSSize(fw, fh))
            frames.append(img)
            delays.append(float(mframes[i].get("delay", 0.06)))
            cheeks.append([tuple(c) for c in mframes[i].get("cheeks", [])])
        if not frames:
            return None
        _log(f"mascot: loaded {len(frames)} frames from {_GIF_PATH}")
        _MASCOT_CACHE = (frames, delays, cheeks, (fw, fh))
        return _MASCOT_CACHE
    except Exception as e:
        _log(f"mascot: load failed: {e}")
        return None


def _jagged(x0, y0, x1, y1, jitter, rng):
    """Midpoint-displacement jagged polyline from (x0,y0)->(x1,y1)."""
    pts = [(x0, y0), (x1, y1)]
    for _ in range(4):
        out = []
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
            nx, ny = -(by - ay), (bx - ax)
            ln = math.hypot(nx, ny) or 1.0
            off = (rng.random() - 0.5) * jitter
            out.append((ax, ay))
            out.append((mx + nx / ln * off, my + ny / ln * off))
        out.append(pts[-1])
        pts = out
        jitter *= 0.55
    return pts


def _polyline_path(pts):
    path = NSBezierPath.bezierPath()
    path.moveToPoint_(NSPoint(pts[0][0], pts[0][1]))
    for (x, y) in pts[1:]:
        path.lineToPoint_(NSPoint(x, y))
    return path


class MascotView(NSView):
    """Draws the looping mascot frame + cheek lightning scaled to mic level."""

    def initWithFrame_(self, frame):
        self = objc.super(MascotView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._level = 0.0          # target level from the recorder
        self._smooth = 0.0         # smoothed level actually rendered
        self._elapsed = 0.0        # seconds since this recording started (drives fill)
        self._mascot = _load_mascot()
        self._idx = 0
        self._accum = 0.0
        self._tick = 0
        self._rng = random.Random(1234)
        return self

    # --- state in --------------------------------------------------------
    def setLevel_(self, level):
        self._level = float(level)

    def advance_(self, timer):
        """NSTimer callback (v@:@) — advance the mascot frame + repaint."""
        interval = 1.0 / 30.0
        # ease the rendered level toward the target so bolts swell/settle
        self._smooth += (self._level - self._smooth) * 0.35
        self._elapsed += interval
        self._tick += 1
        if self._mascot is not None:
            _, delays, _, _ = self._mascot
            self._accum += interval
            # advance as many frames as elapsed (handles slow ticks)
            guard = 0
            while self._accum >= delays[self._idx] and guard < len(delays):
                self._accum -= delays[self._idx]
                self._idx = (self._idx + 1) % len(delays)
                guard += 1
        self.setNeedsDisplay_(True)

    # --- drawing ---------------------------------------------------------
    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(self.bounds())

        b = self.bounds()
        m = MASCOT_SIZE
        # mascot pinned near bottom-centre of the (full-screen) view
        cx = b.size.width / 2.0
        cy = MASCOT_BOTTOM_MARGIN + m / 2.0
        mrect = NSRect(NSPoint(cx - m / 2.0, cy - m / 2.0), NSSize(m, m))

        if self._mascot is None:
            self._draw_fallback(cx, cy, m)
            return

        frames, _, cheeks, _ = self._mascot
        idx = self._idx % len(frames)

        # cheek anchor points in view coords (non-flipped view: y grows up, GIF
        # ny grows down → flip). Draw lightning BEHIND the mascot so bolts read
        # as coming from behind/around it, then the mascot on top.
        anchors = []
        for (nx, ny) in cheeks[idx]:
            ax = mrect.origin.x + nx * m
            ay = mrect.origin.y + (1.0 - ny) * m
            anchors.append((ax, ay))

        # reach: 0 → a tight spark halo, 1 → a slightly bigger halo around the
        # body (still bounded to the mascot, never the whole screen — #276).
        reach = max(0.0, min(1.0, self._elapsed / FILL_SECONDS))
        self._draw_lightning(anchors, cx, cy, m, reach, b.size.width, b.size.height)

        frames[idx].drawInRect_fromRect_operation_fraction_(
            mrect, NSRect(NSPoint(0, 0), frames[idx].size()),
            NSCompositingOperationSourceOver, 1.0,
        )

    def _draw_lightning(self, anchors, cx, cy, span, reach, view_w, view_h):
        # intensity: a always-on idle shimmer + the smoothed mic level
        level = max(0.0, min(1.0, self._smooth))
        intensity = 0.10 + 0.90 * level
        rng = self._rng
        # reseed slowly so bolts crackle but don't hard-strobe every frame
        rng.seed(self._tick // 2 * 977 + 31)

        glow = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.82, 0.16, 0.45)
        midc = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.92, 0.36, 0.95)
        core = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 1.0)

        # Bolts stay a small HALO around the body — never screen-filling (#276).
        # Reach is bounded to a fraction of the mascot size (not the screen
        # diagonal), so the storm crackles around Pikachu without covering the
        # text the user is dictating over.
        reach_len = reach * span * 0.55 * (0.35 + 0.65 * level)
        # keep the fan modest so bolts don't wrap around and cover the screen
        spread_range = math.radians(150 + reach * 40)

        for (ax, ay) in anchors:
            ox, oy = ax - cx, ay - cy
            base_ang = math.atan2(oy, ox) if (ox or oy) else math.pi / 2.0
            n = int(1 + intensity * 5 + reach * 3)   # a few more bolts, not a storm
            for i in range(n):
                spread = (i / max(1, n - 1) - 0.5) * spread_range
                a = base_ang + spread + (rng.random() - 0.5) * 0.4
                length = span * (0.14 + intensity * (0.24 + rng.random() * 0.26)) \
                    + reach_len * (0.45 + 0.55 * rng.random())
                x1 = ax + math.cos(a) * length
                y1 = ay + math.sin(a) * length
                pts = _jagged(ax, ay, x1, y1, length * 0.14 + 3, rng)
                p = _polyline_path(pts)
                p.setLineJoinStyle_(1)   # round
                p.setLineCapStyle_(1)
                glow.set(); p.setLineWidth_(7 + intensity * 4 + reach * 4); p.stroke()
                midc.set(); p.setLineWidth_(3 + intensity * 2 + reach * 2); p.stroke()
                core.set(); p.setLineWidth_(1.5); p.stroke()

            # bright glint sitting on the cheek (the spark source)
            r = span * 0.05 * (0.7 + intensity * 0.6)
            glow.set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSRect(NSPoint(ax - r, ay - r), NSSize(2 * r, 2 * r))).fill()
            core.set()
            rc = r * 0.45
            NSBezierPath.bezierPathWithOvalInRect_(
                NSRect(NSPoint(ax - rc, ay - rc), NSSize(2 * rc, 2 * rc))).fill()

        # on loud peaks, an arc leaps between the two cheeks
        if intensity > 0.75 and len(anchors) == 2:
            (a0x, a0y), (a1x, a1y) = anchors
            mx, my = (a0x + a1x) / 2.0, (a0y + a1y) / 2.0 + span * 0.10
            pts = _jagged(a0x, a0y, mx, my, span * 0.10, rng) + \
                _jagged(mx, my, a1x, a1y, span * 0.10, rng)
            p = _polyline_path(pts)
            p.setLineJoinStyle_(1); p.setLineCapStyle_(1)
            glow.set(); p.setLineWidth_(8); p.stroke()
            midc.set(); p.setLineWidth_(4); p.stroke()
            core.set(); p.setLineWidth_(1.5); p.stroke()

    def _draw_fallback(self, cx, cy, span):
        """No mascot assets — a pulsing yellow dot so recording is never silent."""
        level = max(0.0, min(1.0, self._smooth))
        r = span * 0.16 * (0.7 + level * 0.8)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.85, 0.2, 0.95).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSRect(NSPoint(cx - r, cy - r), NSSize(2 * r, 2 * r))).fill()

    def isFlipped(self):
        return False


class RecordingOverlay:
    """Manages the floating mascot window."""

    def __init__(self):
        self._window = None
        self._view = None
        self._timer = None
        self._visible = False
        self._rebuilt_this_show = False

    def _ensure_window(self):
        if self._window is not None:
            return
        screen = NSScreen.mainScreen()
        # Cover the WHOLE screen (transparent, click-through) so the lightning can
        # grow to fill it. The mascot is drawn near the bottom-centre by the view.
        frame = screen.frame()
        style = (NSWindowStyleMaskBorderless
                 | NSWindowStyleMaskNonactivatingPanel
                 | NSWindowStyleMaskUtilityWindow)
        self._window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False,
        )
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setIgnoresMouseEvents_(True)   # click-through — never blocks the user
        self._window.setHidesOnDeactivate_(False)
        self._window.setFloatingPanel_(True)
        self._window.setCollectionBehavior_((1 << 0) | (1 << 8) | (1 << 4))
        # ORDER MATTERS: setFloatingPanel_(True) forces the level to
        # NSFloatingWindowLevel (3), silently discarding any earlier
        # setLevel_(). Level 3 still wins against ordinary windows (level 0),
        # which is why this hid for so long — but it is nowhere near high
        # enough to clear a native-fullscreen space, so the mascot never drew
        # over Comet. Always set the level LAST (#284).
        self._raise_level()

        self._view = MascotView.alloc().initWithFrame_(
            NSRect(NSPoint(0, 0), frame.size))
        self._window.setContentView_(self._view)

    def _raise_level(self):
        """Force the panel back above every other window.

        Re-asserted on every show() rather than only at creation: AppKit resets
        a panel's level as a side effect of other setters (setFloatingPanel_ is
        the one that bit us), so treating the level as write-once is how it
        silently decays back to 3 (#284)."""
        self._window.setLevel_(ABOVE_ALL_LEVEL)

    def _pin_to_active_screen(self):
        """Move the (full-screen) window onto whatever screen is active RIGHT NOW.

        The window is created once but macOS is multi-monitor: mainScreen() at
        creation time may not be where the user is looking on the next recording,
        and screen.frame() carries a per-display origin. Without this the whole
        overlay stays stranded on the screen it was born on and the mascot never
        appears on the display the user is actually using (#275)."""
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        frame = screen.frame()
        self._window.setFrame_display_(frame, False)
        self._view.setFrameSize_(frame.size)

    def _is_onscreen(self):
        """WindowServer truth: is our panel actually composited on the ACTIVE Space?

        NSWindow.isVisible() is useless here — a wedged window keeps reporting
        visible, keeps accepting setAlphaValue_/orderFrontRegardless, and keeps
        its correct frame and level, while the WindowServer displays nothing.
        Only the on-screen window list knows the difference (#323)."""
        if self._window is None:
            return False
        try:
            wid = self._window.windowNumber()
            if wid <= 0:
                return False
            info = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            ) or []
            return any(w.get("kCGWindowNumber") == wid for w in info)
        except Exception as e:
            # Never let a diagnostic take the overlay down — assume healthy.
            _log(f"onscreen check failed: {e}")
            return True

    def _rebuild_window(self):
        """Throw the panel away and build a fresh one.

        The panel is otherwise created exactly once per process. After enough
        uptime and Space churn macOS can leave it permanently unable to join any
        Space, and no amount of re-asserting screen (#275) or level (#284)
        rescues it — the window itself is the stale thing. Rebuilding is the only
        way out, and it is cheap: the mascot frames are cached module-wide."""
        old = self._window
        self._window = None
        self._view = None
        if old is not None:
            try:
                old.orderOut_(None)
                old.close()
            except Exception as e:
                _log(f"rebuild: closing old window failed: {e}")
        self._ensure_window()

    def _present(self):
        """Raise, pin, reveal, and start the animation timer on the current panel."""
        self._raise_level()
        self._pin_to_active_screen()
        self._window.setAlphaValue_(1.0)
        self._window.orderFrontRegardless()
        if self._timer is not None:
            self._timer.invalidate()
        # NB: the timer targets self._view, so it MUST be re-armed after a
        # rebuild — the old timer would tick a view that is no longer on screen.
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / 30.0, self._view,
            objc.selector(MascotView.advance_, signature=b"v@:@"),
            None, True,
        )

    def _verify_onscreen(self):
        """One-shot rescue, ~a third of a second after show().

        Runs late deliberately: the WindowServer needs a moment to composite, so
        checking inline right after orderFrontRegardless would false-positive."""
        if not self._visible or self._window is None:
            return                      # recording already ended — nothing to rescue
        if self._rebuilt_this_show:
            return                      # one rebuild per recording, never a loop
        if self._is_onscreen():
            return                      # healthy: no rebuild, no flicker
        self._rebuilt_this_show = True
        _log("WEDGED: overlay window never joined the active Space — rebuilding (#323)")
        self._rebuild_window()
        self._view._level = 0.0
        self._view._smooth = 0.0
        self._view._elapsed = 0.0
        self._present()
        _log("rebuild %s" % ("succeeded — mascot is on screen"
                             if self._is_onscreen() else
                             "FAILED — still off screen after rebuild"))

    def show(self):
        if self._visible:
            return

        def _show_on_main():
            _log(f"_show_on_main (thread={threading.current_thread().name})")
            self._ensure_window()
            self._view._level = 0.0
            self._view._smooth = 0.0
            self._view._elapsed = 0.0   # restart the fill ramp each recording
            self._present()
            self._visible = True
            self._rebuilt_this_show = False
            # A wedged window fails silently — every call below succeeds and the
            # log looks perfect while nothing renders. Verify against the
            # WindowServer instead of trusting AppKit (#323).
            from PyObjCTools import AppHelper
            AppHelper.callLater(_ONSCREEN_CHECK_DELAY, self._verify_onscreen)

        if threading.current_thread() is threading.main_thread():
            _show_on_main()
        else:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_show_on_main)

    def hide(self):
        def _hide_on_main():
            _log("_hide_on_main")
            if self._timer:
                self._timer.invalidate()
                self._timer = None
            if self._window:
                self._window.setAlphaValue_(0.0)
                self._window.orderOut_(None)
            self._visible = False

        if threading.current_thread() is threading.main_thread():
            _hide_on_main()
        else:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_hide_on_main)

    def set_level(self, level: float):
        if self._view:
            self._view.setLevel_(level)

    @property
    def visible(self):
        return self._visible
