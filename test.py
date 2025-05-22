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
import objc
import ScreenCaptureKit as SCK
from AVFoundation import (
    AVAssetWriter,
    AVAssetWriterInput,
    AVAssetWriterInputPixelBufferAdaptor,
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
from Quartz import CoreVideo


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
    # Obj-C ivars ----------------------------------------------------------------
    status = objc.ivar()  # NSString*  (we’ll store Python str)
    isRecording = objc.ivar()  # NSNumber*  (Python bool)

    _stream = objc.ivar()  # SCStream*
    _writer = objc.ivar()  # AVAssetWriter*
    _videoInput = objc.ivar()  # AVAssetWriterInput*
    _adaptor = objc.ivar()  # AVAssetWriterInputPixelBufferAdaptor*
    _sessionBegan = objc.ivar()  # NSNumber* / Python bool

    # ---------------------------------------------------------------------------
    #  Initialiser  (Objective-C’s init, *not* __init__)
    # ---------------------------------------------------------------------------
    def init(self):
        self = objc.super(ScreenRecorder, self).init()
        if self is None:
            return None

        self.status = "Idle"
        self.isRecording = False
        self._sessionBegan = False
        return self

    # ---------------------------------------------------------------------------
    #  Public selectors (called from Tkinter wrapper)
    # ---------------------------------------------------------------------------
    def start_(self, callback):
        """Begin an asynchronous capture; `callback(err)` fires on finish/failure."""
        if self.isRecording:
            callback(None)
            return

        def work():
            try:
                self._set_up_capture()
                self.isRecording = True
                callback(None)
            except Exception as exc:
                callback(exc)

        threading.Thread(target=work, daemon=True).start()

    def stop_(self, callback):
        """Stop the capture; `callback(err)` fires when file is finalised."""
        if not self.isRecording or self._stream is None:
            callback(None)
            return

        def finish():
            self._videoInput.markAsFinished()
            self._writer.finishWritingWithCompletionHandler_(
                lambda: self._on_done(callback)
            )

        self._stream.stopCaptureWithCompletionHandler_(lambda _err: finish())

    # ---------------------------------------------------------------------------
    #  Objective-C delegate callbacks
    # ---------------------------------------------------------------------------
    def stream_didOutputSampleBuffer_ofOutputType_(self, _stream, sbuf, outputType):
        if outputType != SCK.SCStreamOutputTypeScreen or not self.isRecording:
            return

        pts = CoreMedia.CMSampleBufferGetPresentationTimeStamp(sbuf)

        if not self._sessionBegan:
            self._writer.startSessionAtSourceTime_(pts)
            self._sessionBegan = True

        pixbuf = CoreMedia.CMSampleBufferGetImageBuffer(sbuf)
        if pixbuf and self._videoInput.isReadyForMoreMediaData():
            ok = self._adaptor.appendPixelBuffer_withPresentationTime_(pixbuf, pts)
            if not ok and self._writer.error():
                print("⚠️ adaptor append failed:", self._writer.error())

    def stream_didStopWithError_(self, _stream, err):
        if err:
            print("Stream stopped with error:", err.localizedDescription())

    # ---------------------------------------------------------------------------
    #  Pure-Python helpers  (HIDDEN from Obj-C via @objc.python_method)
    # ---------------------------------------------------------------------------
    @objc.python_method
    def _set_up_capture(self):
        # 1️⃣ discover display + build filter
        try:
            # macOS 14+  (sync API)
            content, err = (
                SCK.SCShareableContent.shareableContentExcludingDesktopWindows_onScreenWindowsOnly_error_(  # type: ignore[attr-defined]
                    False, True, None
                )
            )
        except AttributeError:
            # macOS 13  (async API) – wrap in an Event so we can block
            done = threading.Event()
            box = {}

            def handler(c, e):
                box["content"] = c
                box["err"] = e
                done.set()

            (
                SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                    False, True, handler
                )
            )
            done.wait()
            content = box["content"]
            err = box["err"]

        if err or not content.displays():
            raise RuntimeError(
                err.localizedDescription() if err else "No display found"
            )

        display = content.displays()[0]
        excluded = {"com.apple.MobileSMS", "com.apple.iChat"}
        included = [
            app
            for app in content.applications()
            if app.bundleIdentifier() not in excluded
        ]

        alloc = SCK.SCContentFilter.alloc()
        if hasattr(alloc, "initWithDisplay_includingApplications_exceptingWindows_"):
            # macOS 13 / Ventura
            filter_ = alloc.initWithDisplay_includingApplications_exceptingWindows_(
                display, included, None
            )
        elif hasattr(alloc, "initWithDisplay_includingWindows_exceptingWindows_"):
            # Rare transitional beta; very similar signature
            filter_ = alloc.initWithDisplay_includingWindows_exceptingWindows_(
                display, included, None
            )
        else:
            # Fallback – capture the whole display, just exclude nothing
            filter_ = alloc.initWithDisplay_excludingWindows_(display, None)

        # 2️⃣ stream configuration
        cfg = SCK.SCStreamConfiguration.new()

        width_val = int(display.width())  # plain Python ints
        height_val = int(display.height())

        # cfg.setWidth_(width_val)
        # cfg.setHeight_(height_val)
        # cfg.pixelFormat = CoreVideo.kCVPixelFormatType_32BGRA
        # cfg.scalesToFit = False

        # 3️⃣ output file URL
        movies = Path(".")
        movies.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds").replace(":", "-")
        url = movies / f"Screen-{stamp}.mov"

        # 4️⃣ asset writer + input
        nsurl = NSURL.fileURLWithPath_(str(url))
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

        self._adaptor = AVAssetWriterInputPixelBufferAdaptor.alloc().initWithAssetWriterInput_sourcePixelBufferAttributes_(
            self._videoInput,
            {
                CoreVideo.kCVPixelBufferPixelFormatTypeKey: cfg.pixelFormat,
                CoreVideo.kCVPixelBufferWidthKey: cfg.width,
                CoreVideo.kCVPixelBufferHeightKey: cfg.height,
            },
        )

        if not self._writer.startWriting():
            raise RuntimeError(f"Writer error: {self._writer.error()}")

        # 5️⃣ ScreenCaptureKit stream
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            filter_, cfg, self
        )
        self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self, SCK.SCStreamOutputTypeScreen, None, None
        )

        # macOS 14 – synchronous API
        if hasattr(self._stream, "startCaptureAndReturnError_"):
            ok, err = self._stream.startCaptureAndReturnError_(None)
            if not ok:
                raise RuntimeError(
                    err.localizedDescription() if err else "startCapture failed"
                )

        # macOS 13 – asynchronous API
        else:
            done = threading.Event()
            box = {}

            def handler(err):
                box["err"] = err
                done.set()

            self._stream.startCaptureWithCompletionHandler_(handler)
            done.wait()
            if box["err"]:
                raise RuntimeError(box["err"].localizedDescription())

        ok, err = self._stream.startCaptureAndReturnError_(None)
        if not ok:
            raise RuntimeError(
                err.localizedDescription() if err else "startCapture failed"
            )

        self.status = f"Recording → {url.name}"
        print("📹  Recording to", url)

    @objc.python_method
    def _on_done(self, cb):
        self.status = "Saved in Movies folder"
        self.isRecording = False
        self._sessionBegan = False
        print("✅  File finalised")
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
