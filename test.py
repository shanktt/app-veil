#!/usr/bin/env python3
"""
screen_recorder.py
------------------
Minimal ScreenCaptureKit recorder with a Tkinter UI:
• “Start” – begins a capture of the main display, writing a .mov into ~/Movies
• “Stop”  – ends the capture and finalises the file
The Messages and iMessage apps are excluded from the recording, just like the
original Swift version.
"""

import datetime
import threading
import tkinter as tk
from pathlib import Path

import CoreMedia
import libdispatch  # <-- NEW
import objc
import ScreenCaptureKit as SCK
from AVFoundation import (
    AVAssetWriter,
    AVAssetWriterInput,
    AVVideoAverageBitRateKey,
    AVVideoCodecKey,
    AVVideoCodecTypeH264,
    AVVideoCompressionPropertiesKey,
    AVVideoHeightKey,
    AVVideoMaxKeyFrameIntervalKey,
    AVVideoProfileLevelH264HighAutoLevel,
    AVVideoProfileLevelKey,
    AVVideoWidthKey,
)
from Foundation import NSURL, NSObject


# ---------------------------------------------------------------------------
#  ScreenRecorder – Objective-C bridge for ScreenCaptureKit + AVFoundation
# ---------------------------------------------------------------------------
class ScreenRecorder(
    NSObject,
    protocols=[
        objc.protocolNamed("SCStreamOutput"),
        objc.protocolNamed("SCStreamDelegate"),
    ],
):
    # Obj-C ivars ------------------------------------------------------------
    status = objc.ivar()
    isRecording = objc.ivar()

    _stream = objc.ivar()
    _writer = objc.ivar()
    _videoInput = objc.ivar()
    # _adaptor = objc.ivar()
    _sessionBegan = objc.ivar()

    # -----------------------------------------------------------------------
    def init(self):
        self = objc.super(ScreenRecorder, self).init()
        if self is None:
            return None
        self.status = "Idle"
        self.isRecording = False
        self._sessionBegan = False
        return self

    # -----------------------------------------------------------------------
    #  Public selectors (UI calls these)
    # -----------------------------------------------------------------------
    def start_(self, cb):
        if self.isRecording:
            cb(None)
            return

        def work():
            try:
                self._setup_capture()
                self.isRecording = True
                cb(None)
            except Exception as exc:
                cb(exc)

        threading.Thread(target=work, daemon=True).start()

    def stop_(self, cb):
        if not self.isRecording or self._stream is None:
            cb(None)
            return

        def finish():
            self._videoInput.markAsFinished()
            self._writer.finishWritingWithCompletionHandler_(lambda: self._on_done(cb))

        self._stream.stopCaptureWithCompletionHandler_(lambda _err: finish())

    # -----------------------------------------------------------------------
    #  Delegate callbacks
    # -----------------------------------------------------------------------
    def stream_didOutputSampleBuffer_ofOutputType_(self, _s, sbuf, otype):
        print("frame")
        if otype != SCK.SCStreamOutputTypeScreen or not self.isRecording:
            return

        pts = CoreMedia.CMSampleBufferGetPresentationTimeStamp(sbuf)

        if not self._sessionBegan:
            self._writer.startSessionAtSourceTime_(pts)
            self._sessionBegan = True

        # Append the *entire* sample buffer
        if self._videoInput.isReadyForMoreMediaData():
            ok = self._videoInput.appendSampleBuffer_(sbuf)
            if not ok and self._writer.error():
                print("⚠️ append failed:", self._writer.error())

    def stream_didStopWithError_(self, _s, err):
        if err:
            print("Stream stopped with error:", err.localizedDescription())

    # -----------------------------------------------------------------------
    #  Pure-Python helpers
    # -----------------------------------------------------------------------
    @objc.python_method
    def _setup_capture(self):
        # 1️⃣  Shareable content (sync API on macOS 14, async on 13)
        try:
            content, err = (
                SCK.SCShareableContent.shareableContentExcludingDesktopWindows_onScreenWindowsOnly_error_(  # macOS 14
                    False, True, None
                )
            )
        except AttributeError:
            done = threading.Event()
            box = {}

            def handler(c, e):
                box["c"], box["e"] = c, e
                done.set()

            (
                SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(  # macOS 13
                    False, True, handler
                )
            )
            done.wait()
            content, err = box["c"], box["e"]

        if err or not content.displays():
            raise RuntimeError(
                err.localizedDescription() if err else "No display found"
            )

        display = content.displays()[0]

        # 2️⃣  Content filter (API names differ)
        excluded = {"com.apple.MobileSMS", "com.apple.iChat"}
        included = [
            app
            for app in content.applications()
            if app.bundleIdentifier() not in excluded
        ]

        alloc = SCK.SCContentFilter.alloc()
        if hasattr(alloc, "initWithDisplay_includingApplications_exceptingWindows_"):
            filter_ = alloc.initWithDisplay_includingApplications_exceptingWindows_(
                display, included, None
            )
        else:  # fallback – capture whole display
            filter_ = alloc.initWithDisplay_excludingWindows_(display, None)

        # 3️⃣  Stream configuration
        cfg = SCK.SCStreamConfiguration.new()
        width_val = int(display.width())
        height_val = int(display.height())
        cfg.setWidth_(width_val)
        cfg.setHeight_(height_val)
        # cfg.pixelFormat = CoreVideo.kCVPixelFormatType_32BGRA
        # cfg.scalesToFit = False

        # 4️⃣  Writer + input
        movies = Path(".")
        movies.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds").replace(":", "-")
        out = movies / f"Screen-{stamp}.mov"

        nsurl = NSURL.fileURLWithPath_(str(out))
        self._writer, err = AVAssetWriter.alloc().initWithURL_fileType_error_(
            nsurl, "com.apple.quicktime-movie", None
        )
        if err:
            raise RuntimeError(err.localizedDescription())

        compression = {
            AVVideoAverageBitRateKey: 5_000_000,
            AVVideoMaxKeyFrameIntervalKey: 30,
            AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
        }
        settings = {
            AVVideoCodecKey: AVVideoCodecTypeH264,
            AVVideoWidthKey: width_val,
            AVVideoHeightKey: height_val,
            AVVideoCompressionPropertiesKey: compression,
        }
        self._videoInput = AVAssetWriterInput.alloc().initWithMediaType_outputSettings_(
            "vide", settings
        )
        self._videoInput.setExpectsMediaDataInRealTime_(True)
        if self._writer.canAddInput_(self._videoInput):
            self._writer.addInput_(self._videoInput)
        else:
            raise RuntimeError("Cannot add video input")

        if not self._writer.startWriting():
            raise RuntimeError(self._writer.error())

        # 5️⃣  Build & start stream (sync on 14, async on 13)
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            filter_, cfg, self
        )
        self._queue = libdispatch.dispatch_queue_create(b"ScreenStream", None)

        self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self,  # implements SCStreamOutput
            SCK.SCStreamOutputTypeScreen,
            self._queue,  # ← real dispatch_queue_t
            None,
        )

        if hasattr(self._stream, "startCaptureAndReturnError_"):  # macOS 14
            ok, err = self._stream.startCaptureAndReturnError_(None)
            if not ok:
                raise RuntimeError(
                    err.localizedDescription() if err else "start failed"
                )
        else:  # macOS 13
            done = threading.Event()
            box = {}

            def handler(e):
                box["err"] = e
                done.set()

            self._stream.startCaptureWithCompletionHandler_(handler)
            done.wait()
            if box["err"]:
                raise RuntimeError(box["err"].localizedDescription())

        self.status = f"Recording → {out.name}"
        print("📹 Recording to", out)

    @objc.python_method
    def _on_done(self, cb):
        self.status = "Saved in Movies folder"
        self.isRecording = False
        self._sessionBegan = False
        print("✅ File finalised")
        cb(None)


# ------------------------------------------------------------
#  Very small Tkinter UI
# ------------------------------------------------------------


class RecorderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen Recorder")

        # Tk variables bound to the objective-C ivars
        self.status_var = tk.StringVar(value="Idle")
        self.recorder = ScreenRecorder.alloc().init()

        # --- Layout ---------------------------------------------------------
        tk.Label(
            root,
            textvariable=self.status_var,
            font=("Helvetica", 14, "bold"),
            wraplength=320,
        ).pack(pady=12)

        btn_frame = tk.Frame(root)
        btn_frame.pack()

        self.start_btn = tk.Button(
            btn_frame, text="Start", width=10, command=self._start
        )
        self.start_btn.grid(row=0, column=0, padx=8)

        self.stop_btn = tk.Button(
            btn_frame, text="Stop", width=10, state="disabled", command=self._stop
        )
        self.stop_btn.grid(row=0, column=1, padx=8)

        root.geometry("360x140")

        # Poll the status from the Objective-C side every 200 ms
        self._sync_ui()

    # ----------------------------------------------------------------------

    def _sync_ui(self):
        self.status_var.set(str(self.recorder.status))
        running = bool(self.recorder.isRecording)
        self.start_btn.config(state=("disabled" if running else "normal"))
        self.stop_btn.config(state=("normal" if running else "disabled"))
        self.root.after(200, self._sync_ui)

    # ----------------------------------------------------------------------

    def _start(self):
        def done(err):
            if err:
                print(err)
                self.status_var.set(f"Failed: {err}")

        self.recorder.start_(done)

    def _stop(self):
        self.recorder.stop_(lambda err: None)


# ------------------------------------------------------------
#  Entrypoint
# ------------------------------------------------------------


def main():
    root = tk.Tk()
    app = RecorderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
