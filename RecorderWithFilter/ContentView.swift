// RecorderApp.swift

import SwiftUI
import ScreenCaptureKit
import AVFoundation

@main
struct RecorderApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

// MARK: – UI

struct ContentView: View {
    @StateObject private var recorder = ScreenRecorder()

    var body: some View {
        VStack(spacing: 24) {
            Text(recorder.status)
                .font(.system(.headline, design: .rounded))
                .lineLimit(2)

            HStack(spacing: 20) {
                Button("Start")  { Task { await recorder.start() } }
                    .disabled(recorder.isRecording)

                Button("Stop")   { recorder.stop() }
                    .disabled(!recorder.isRecording)
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(32)
        .frame(width: 360, height: 160)
    }
}

// MARK: – Recorder

@MainActor
final class ScreenRecorder: NSObject, ObservableObject {
    @Published var status      = "Idle"
    @Published var isRecording = false

    private var stream:    SCStream?
    private var writer:    AVAssetWriter?
    private var videoIn:   AVAssetWriterInput?
    private var adaptor:   AVAssetWriterInputPixelBufferAdaptor?
    private var sessionStarted = false

    func start() async {
        guard !isRecording else { return }

        do {
            // 1️⃣ Discover display + build filter
            let content  = try await SCShareableContent.excludingDesktopWindows(false,
                                                                                   onScreenWindowsOnly: true)
            guard let display = content.displays.first else {
                status = "No display found"; return
            }

            let excludedIDs = ["com.apple.MobileSMS", "com.apple.iChat"]
            let includedApps = content.applications.filter {
                !excludedIDs.contains($0.bundleIdentifier)
            }
            let filter = SCContentFilter(
                display: display,
                including: includedApps,
                exceptingWindows: []
            )

            // 2️⃣ Stream config
            let cfg = SCStreamConfiguration()
            cfg.width       = display.width
            cfg.height      = display.height
            cfg.pixelFormat = kCVPixelFormatType_32BGRA
            cfg.scalesToFit = false

            // 3️⃣ Ensure Movies/ exists
            let moviesURL = FileManager.default.urls(for: .moviesDirectory,
                                                     in: .userDomainMask)[0]
            try FileManager.default.createDirectory(at: moviesURL,
                                                    withIntermediateDirectories: true)

            // 4️⃣ Build AVAssetWriter + Input
            let stamp = ISO8601DateFormatter()
                            .string(from: Date())
                            .replacingOccurrences(of: ":", with: "-")
            let fileURL = moviesURL.appendingPathComponent("Screen-\(stamp).mov")

            writer = try AVAssetWriter(outputURL: fileURL, fileType: .mov)

            // compression settings
            let compressionProps: [String: Any] = [
                AVVideoAverageBitRateKey:      5_000_000,
                AVVideoMaxKeyFrameIntervalKey: 30,
                AVVideoProfileLevelKey:        AVVideoProfileLevelH264HighAutoLevel
            ]
            let videoSettings: [String: Any] = [
                AVVideoCodecKey:                AVVideoCodecType.h264,
                AVVideoWidthKey:                cfg.width,
                AVVideoHeightKey:               cfg.height,
                AVVideoCompressionPropertiesKey: compressionProps
            ]

            videoIn = AVAssetWriterInput(mediaType: .video,
                                         outputSettings: videoSettings)
            videoIn?.expectsMediaDataInRealTime = true

            if let videoIn = videoIn, writer!.canAdd(videoIn) {
                writer!.add(videoIn)
            } else {
                throw NSError(domain: "WriterConfig", code: -1,
                              userInfo: [NSLocalizedDescriptionKey: "Cannot add video input"])
            }

            // set up pixel-buffer adaptor
            adaptor = AVAssetWriterInputPixelBufferAdaptor(
                assetWriterInput: videoIn!,
                sourcePixelBufferAttributes: [
                    kCVPixelBufferPixelFormatTypeKey as String: cfg.pixelFormat,
                    kCVPixelBufferWidthKey as String:            cfg.width,
                    kCVPixelBufferHeightKey as String:           cfg.height
                ]
            )

            guard writer!.startWriting() else {
                throw writer!.error ?? NSError(domain: "WriterStart", code: -1)
            }
            // — DO NOT call startSession here —

            // 5️⃣ Kick off capture
            let streamQueue = DispatchQueue(label: "ScreenStream")
            stream = SCStream(filter: filter, configuration: cfg, delegate: self)
            try stream?.addStreamOutput(self,
                                       type: .screen,
                                       sampleHandlerQueue: streamQueue)
            try await stream!.startCapture()

            print("📹 Recording to:", fileURL.path)
            status      = "Recording → \(fileURL.lastPathComponent)"
            isRecording = true

        } catch {
            status = "Failed: \(error.localizedDescription)"
        }
    }

    func stop() {
        guard isRecording, let stream = stream else { return }
        stream.stopCapture { [weak self] _ in
            guard let self = self else { return }
            self.videoIn?.markAsFinished()
            self.writer?.finishWriting {
                DispatchQueue.main.async {
                    self.status         = "Saved in Movies folder"
                    self.isRecording    = false
                    self.sessionStarted = false
                }
            }
        }
    }
}

// MARK: – Frame callback

extension ScreenRecorder: SCStreamOutput, SCStreamDelegate {
    func stream(_ stream: SCStream,
                didOutputSampleBuffer sbuf: CMSampleBuffer,
                of outputType: SCStreamOutputType)
    {
        guard outputType == .screen,
              isRecording,
              let videoIn = videoIn,
              let adaptor  = adaptor,
              videoIn.isReadyForMoreMediaData else { return }

        let pts = CMSampleBufferGetPresentationTimeStamp(sbuf)

        if !sessionStarted {
            writer?.startSession(atSourceTime: pts)
            sessionStarted = true
            print("🎬 started session at", pts)
        }

        // extract CVPixelBuffer & append via adaptor
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sbuf) else { return }
        if !adaptor.append(pixelBuffer, withPresentationTime: pts) {
            let nsErr = writer!.error as NSError?
            print("⚠️ adaptor append failed:",
                  "status=\(writer!.status)",
                  "domain=\(nsErr?.domain ?? "")",
                  "code=\(nsErr?.code ?? 0)",
                  "info=\(nsErr?.userInfo ?? [:])")
        }
    }
}
