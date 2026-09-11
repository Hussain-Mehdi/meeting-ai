import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

final class WaveWriter {
    private let writer: AVAssetWriter
    private let input: AVAssetWriterInput
    private var started = false

    init(url: URL, channels: Int) throws {
        try? FileManager.default.removeItem(at: url)
        writer = try AVAssetWriter(outputURL: url, fileType: .wav)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: channels,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false
        ]
        input = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else { throw NSError(domain: "MeetingAudioCapture", code: 1, userInfo: [NSLocalizedDescriptionKey: "Unable to create WAV writer"])}
        writer.add(input)
    }

    func append(_ sample: CMSampleBuffer) {
        if !started {
            writer.startWriting()
            writer.startSession(atSourceTime: sample.presentationTimeStamp)
            started = true
        }
        if input.isReadyForMoreMediaData { input.append(sample) }
    }

    func finish() async {
        guard started else { writer.cancelWriting(); return }
        input.markAsFinished()
        await writer.finishWriting()
    }
}

final class CaptureOutput: NSObject, SCStreamOutput, SCStreamDelegate {
    let systemWriter: WaveWriter
    let microphoneWriter: WaveWriter

    init(systemURL: URL, microphoneURL: URL) throws {
        systemWriter = try WaveWriter(url: systemURL, channels: 2)
        microphoneWriter = try WaveWriter(url: microphoneURL, channels: 1)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard sampleBuffer.isValid else { return }
        if type == .audio { systemWriter.append(sampleBuffer) }
        if type == .microphone { microphoneWriter.append(sampleBuffer) }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        // Flush what was captured so far and exit, so the backend notices the dead
        // stream instead of showing "Recording" while the files stop growing.
        fputs("Capture stopped: \(error.localizedDescription)\n", stderr)
        Task {
            await systemWriter.finish()
            await microphoneWriter.finish()
            exit(3)
        }
    }
}

@main
struct MeetingAudioCapture {
    static func main() async {
        guard CommandLine.arguments.count == 3 else {
            fputs("Usage: meeting-audio-capture <system.wav> <microphone.wav>\n", stderr)
            exit(2)
        }
        do {
            let systemURL = URL(fileURLWithPath: CommandLine.arguments[1])
            let microphoneURL = URL(fileURLWithPath: CommandLine.arguments[2])
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else { throw NSError(domain: "MeetingAudioCapture", code: 2, userInfo: [NSLocalizedDescriptionKey: "No display is available for system-audio capture"]) }
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let configuration = SCStreamConfiguration()
            configuration.width = 2
            configuration.height = 2
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            configuration.showsCursor = false
            configuration.capturesAudio = true
            configuration.excludesCurrentProcessAudio = true
            configuration.sampleRate = 48_000
            configuration.channelCount = 2
            configuration.captureMicrophone = true

            let output = try CaptureOutput(systemURL: systemURL, microphoneURL: microphoneURL)
            let stream = SCStream(filter: filter, configuration: configuration, delegate: output)
            let systemQueue = DispatchQueue(label: "meeting-ai.system-audio")
            let microphoneQueue = DispatchQueue(label: "meeting-ai.microphone")
            try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: systemQueue)
            try stream.addStreamOutput(output, type: .microphone, sampleHandlerQueue: microphoneQueue)

            signal(SIGINT, SIG_IGN)
            signal(SIGTERM, SIG_IGN)
            let signalStream = AsyncStream<Void> { continuation in
                let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
                let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
                interrupt.setEventHandler { continuation.finish() }
                terminate.setEventHandler { continuation.finish() }
                interrupt.resume(); terminate.resume()
                continuation.onTermination = { _ in interrupt.cancel(); terminate.cancel() }
            }

            try await stream.startCapture()
            print("READY", terminator: "\n")
            fflush(stdout)
            for await _ in signalStream { }
            try await stream.stopCapture()
            await output.systemWriter.finish()
            await output.microphoneWriter.finish()
        } catch {
            fputs("Meeting audio capture failed: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
}
