#!/usr/bin/env pythonw
"""
Tuple-style “App Veil” for macOS screen-recorders
-------------------------------------------------

* Reads a blacklist of bundle-IDs from `~/.myrecorder-veil.toml`
* Drops translucent, always-on-top windows to hide them
* Keeps the overlays in-sync as the real windows move / resize

❏  Requirements
    brew install python        # or use the system Python
    pip install pyobjc tomli   # macOS GUI + TOML reader (tomllib built-in ≥3.11)

❏  Example ~/.myrecorder-veil.toml
    [veil]
    apps         = ["com.apple.MobileSMS", "com.tinyspeck.slackmacgap"]
    border_color = "#A0A0A080"
    border_dash  = [6,4]
    fill_color   = "#000000F0"
"""

import pathlib
import sys
import tomllib
from functools import lru_cache

import Foundation
import Quartz  # gives us CAShapeLayer & CGPath helpers
from AppKit import (
    NSBackingStoreBuffered,
    NSMakeRect,
    NSScreen,
    NSStatusWindowLevel,
    NSWindow,
    NSWindowStyleMaskBorderless,
)
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGNullWindowID,
    kCGWindowListOptionOnScreenOnly,
)

CFG_PATH = pathlib.Path("config.toml")
if CFG_PATH.exists():
    _raw = tomllib.loads(CFG_PATH.read_text())["veil"]
else:  # sane defaults
    _raw = dict(
        apps=[],
        border_color="#A0A0A080",
        border_dash=[4, 4],
        fill_color="#000000F0",
    )

BLACKLIST = set(_raw["apps"])
BORDER_COLOR_H = _raw["border_color"]
BORDER_DASH = _raw["border_dash"]
FILL_COLOR_H = _raw["fill_color"]

# ---------- 2. helpers ---------------------------------------------------------


def cg_to_ns_rect(bounds: dict) -> Foundation.NSRect:
    """
    Convert a CoreGraphics kCGWindowBounds dict to an NSRect usable for NSWindow.
    Works for the primary display; for multi-monitor setups see note below.
    """
    screen = NSScreen.mainScreen().frame()  # full virtual height
    flipped_y = screen.size.height - bounds["Y"] - bounds["Height"]
    return NSMakeRect(bounds["X"], flipped_y, bounds["Width"], bounds["Height"])


def _hex_to_rgba(h: str):
    h = h.lstrip("#")
    r, g, b, a = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4, 6))
    return r, g, b, a


@lru_cache(maxsize=None)
def nscolor_from_hex(h: str):
    from AppKit import NSColor

    r, g, b, a = _hex_to_rgba(h)
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


# ---------- 3. Quartz window enumeration --------------------------------------


def blacklisted_windows():
    """Yield (window_id, bounds dict) for every on-screen window we must hide"""
    data = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    for win in data:
        bundle = win.get("kCGWindowOwnerName")
        if bundle and bundle in BLACKLIST:
            yield win["kCGWindowNumber"], win["kCGWindowBounds"]


# ---------- 4. overlay window --------------------------------------------------


class VeilWindow(NSWindow):
    """
    Thin Objective-C subclass that paints a translucent rectangle + dashed border.
    Call VeilWindow.create(bounds) to get an instance.
    """

    @classmethod
    def create_(cls, bounds):
        frame = cg_to_ns_rect(bounds)
        # 👈  Proper ObjC allocation
        self = cls.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        if self is None:  # init can theoretically fail
            return None

        self.setLevel_(NSStatusWindowLevel + 5)  # float above everything
        self.setIgnoresMouseEvents_(True)
        self.setOpaque_(False)
        self.setBackgroundColor_(Foundation.NSColor.clearColor())
        self._decorate()
        return self  # ← finished window object

    # -------- visuals -------------------------------------------------

    def _decorate(self):
        cv = self.contentView()
        cv.setWantsLayer_(True)

        # ---------- opaque fill --------------------------------------------------
        # cv.layer().setBackgroundColor_(nscolor_from_hex(FILL_COLOR_H).CGColor())

        # ---------- dashed border -------------------------------------------------
        shape = Quartz.CAShapeLayer.layer()
        shape.setFrame_(cv.bounds())
        shape.setLineWidth_(10.0)
        shape.setStrokeColor_(nscolor_from_hex(BORDER_COLOR_H).CGColor())

        shape.setFillColor_(Quartz.CGColorGetConstantColor(Quartz.kCGColorClear))

        nums = [Foundation.NSNumber.numberWithInt_(d) for d in BORDER_DASH]
        shape.setLineDashPattern_(Foundation.NSArray.arrayWithArray_(nums))

        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathAddRect(path, None, cv.bounds())
        shape.setPath_(path)

        cv.layer().addSublayer_(shape)

    # convenience for resize / move
    def sync_frame(self, bounds):
        self.setFrame_display_(cg_to_ns_rect(bounds), False)


# ---------- 5. main loop -------------------------------------------------------

from AppKit import NSApplication, NSDate, NSRunLoop


def main():
    app = NSApplication.sharedApplication()  # needed for GUI windows
    veil_windows = {}

    while True:
        current = dict(blacklisted_windows())

        # create / move overlays
        for win_id, bounds in current.items():
            if win_id not in veil_windows:
                vw = VeilWindow.create_(bounds)
                veil_windows[win_id] = vw
                vw.makeKeyAndOrderFront_(None)
            else:
                veil_windows[win_id].sync_frame(bounds)

        # remove vanished overlays
        for win_id in set(veil_windows) - set(current):
            veil_windows[win_id].orderOut_(None)
            del veil_windows[win_id]

        # keep the Cocoa runloop alive without blocking
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.01)
        )


if __name__ == "__main__":
    if not BLACKLIST:
        print(
            "⚠️  No apps in blacklist — edit ~/.myrecorder-veil.toml first",
            file=sys.stderr,
        )
    main()
