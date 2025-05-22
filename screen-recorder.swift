#!/usr/bin/swift
import Foundation
import ScreenCaptureKit
import AVFoundation

// -----------------------------------------------------------------------------
// MARK: – ScreenRecorder
// -----------------------------------------------------------------------------
@MainActor
final class ScreenRecorder: NSObject {

    private let excludedIDs: Set<String>
    private var stream:    SCStream?
    private var writer:    AVAssetWriter?
    private var videoIn:   AVAssetWriterInput?
    private var adaptor:   AVAssetWriterInputPixelBufferAdaptor?
    private var sessionStarted = false

    init(excluding ids: [String]) { self.excludedIDs = Set(ids) }

    // record exactly 30 s
    func recordFor30s() async throws {
        try await startCapture()
        try await Task.sleep(nanoseconds: 30 * 1_000_000_000)
        try await stopCapture()
    }

    // -------------------------------------------------------------------------
    // MARK: – Capture plumbing
    // -------------------------------------------------------------------------
    private func startCapture() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false,
                                                                           onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            throw NSError(domain: "ScreenRecorder", code: 0,
                          userInfo:[NSLocalizedDescriptionKey:"No display found"])
        }

        let filter = SCContentFilter(
            display: display,
            including: content.applications.filter { !excludedIDs.contains($0.bundleIdentifier) },
            exceptingWindows: [])

        let cfg = SCStreamConfiguration()
        cfg.width = display.width
        cfg.height = display.height
        cfg.pixelFormat = kCVPixelFormatType_32BGRA
        cfg.scalesToFit = false

        // Movies/Screen-<ts>.mov
        let movies = FileManager.default.urls(for: .moviesDirectory, in: .userDomainMask)[0]
        try FileManager.default.createDirectory(at: movies, withIntermediateDirectories: true)
        let ts = ISO8601DateFormatter().string(from: .init()).replacingOccurrences(of: ":", with: "-")
        let fileURL = movies.appendingPathComponent("Screen-\(ts).mov")

        writer = try AVAssetWriter(outputURL: fileURL, fileType: .mov)

        let videoSettings: [String:Any] = [
            AVVideoCodecKey:  AVVideoCodecType.h264,
            AVVideoWidthKey:  cfg.width,
            AVVideoHeightKey: cfg.height,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: 5_000_000,
                AVVideoMaxKeyFrameIntervalKey: 30,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel
            ]
        ]
        videoIn = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
        videoIn?.expectsMediaDataInRealTime = true
        guard let videoIn, let writer, writer.canAdd(videoIn) else {
            throw NSError(domain:"ScreenRecorder",code:1,
                          userInfo:[NSLocalizedDescriptionKey:"Cannot add writer input"])
        }
        writer.add(videoIn)

        adaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: videoIn,
            sourcePixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: cfg.pixelFormat,
                kCVPixelBufferWidthKey  as String: cfg.width,
                kCVPixelBufferHeightKey as String: cfg.height
            ])

        guard writer.startWriting() else { throw writer.error! }

        let q = DispatchQueue(label: "ScreenStream")
        stream = SCStream(filter: filter, configuration: cfg, delegate: self)
        try stream?.addStreamOutput(self, type: .screen, sampleHandlerQueue: q)
        try await stream?.startCapture()

        print("📹 Recording for 30 s → \(fileURL.lastPathComponent)")
    }

    private func stopCapture() async throws {
        guard let stream else { return }

        try await withCheckedThrowingContinuation { cont in
            stream.stopCapture { err in
                err == nil ? cont.resume() : cont.resume(throwing: err!)
            }
        }

        videoIn?.markAsFinished()
        writer?.finishWriting { [writer] in      // capture local `writer`
            Task { @MainActor in
                if let w = writer, w.status == .completed {
                    print("✅ Saved to \(w.outputURL.path)")
                } else {
                    print("⚠️ Writer finished with status \(writer?.status.rawValue ?? -1)")
                }
                CFRunLoopStop(CFRunLoopGetMain())
            }
        }
    }
}

// -----------------------------------------------------------------------------
// MARK: – SCStream callbacks (stay on Main-actor)
// -----------------------------------------------------------------------------
extension ScreenRecorder: @preconcurrency SCStreamOutput, @preconcurrency SCStreamDelegate {
    func stream(_ stream: SCStream,
                didOutputSampleBuffer sbuf: CMSampleBuffer,
                of outputType: SCStreamOutputType)
    {
        guard outputType == .screen,
              let videoIn, let adaptor,
              videoIn.isReadyForMoreMediaData,
              let px = CMSampleBufferGetImageBuffer(sbuf) else { return }

        let pts = CMSampleBufferGetPresentationTimeStamp(sbuf)
        if !sessionStarted {
            writer?.startSession(atSourceTime: pts)
            sessionStarted = true
        }
        if !adaptor.append(px, withPresentationTime: pts),
           let err = writer?.error {
            fputs("⚠️ append failed: \(err.localizedDescription)\n", stderr)
        }
    }
}

// -----------------------------------------------------------------------------
// MARK: – Kick-off
// -----------------------------------------------------------------------------
Task {
    do {
        let recorder = await ScreenRecorder(excluding: Array(CommandLine.arguments.dropFirst()))
        try await recorder.recordFor30s()
    } catch { fputs("❌ \(error)\n", stderr); CFRunLoopStop(CFRunLoopGetMain()) }
}

// keep the process alive
dispatchMain()

