import argparse
import re
import subprocess
import time
from collections import deque
from pathlib import Path
from threading import Lock, Thread
from .devices import AudioDeviceManager


class RecordingError(RuntimeError): pass


class AudioRecorder:
    def __init__(self, devices=None):
        self.devices = devices or AudioDeviceManager()
        self.process = None
        self.started_monotonic = None
        self.path = None
        self.source_paths = []
        self.capture_backend = "unknown"
        self._lock = Lock()
        self._stderr_lines = deque(maxlen=50)

    def _drain_stderr(self, process) -> None:
        """Keep the helper's stderr pipe empty so it can never block on a full buffer."""
        def pump():
            try:
                for raw in iter(process.stderr.readline, b""):
                    line = raw.decode(errors="replace").rstrip()
                    if line: self._stderr_lines.append(line)
            except Exception: pass
        Thread(target=pump, name="audio-capture-stderr", daemon=True).start()

    def _recent_stderr(self) -> str:
        return " | ".join(self._stderr_lines).strip()

    def health(self) -> str | None:
        """Return a description of the failure if the capture process died, else None."""
        if not self.process or self.started_monotonic is None: return None
        code = self.process.poll()
        if code is None: return None
        detail = self._recent_stderr() or f"exit code {code}"
        return f"Audio capture process stopped unexpectedly ({detail})."

    def start(self, path: Path) -> None:
        with self._lock:
            if self.process and self.process.poll() is None: raise RecordingError("A recording is already in progress.")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.path = path
            system_path = path.parent / "recording-system.wav"
            microphone_path = path.parent / "recording-microphone.wav"
            self.source_paths = [system_path]
            self._stderr_lines.clear()
            native_helper = Path("bin/meeting-audio-capture")
            if native_helper.exists() and native_helper.is_file():
                self.source_paths = [system_path, microphone_path]
                self.capture_backend = "ScreenCaptureKit"
                self.process = subprocess.Popen([str(native_helper), str(system_path), str(microphone_path)],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self._drain_stderr(self.process)
                time.sleep(1.0)
                if self.process.poll() is not None:
                    time.sleep(0.2)
                    error = self._recent_stderr()
                    raise RecordingError(f"Native macOS audio capture could not start: {error} Grant Screen & System Audio Recording and Microphone permissions, then restart Meeting AI.")
                self.started_monotonic = time.monotonic()
                return
            self.capture_backend = "BlackHole fallback"
            device = self.devices.find_blackhole()
            if not device: raise RecordingError("Native capture is unavailable and BlackHole 2ch was not detected. Re-run setup or verify BlackHole in Audio MIDI Setup.")
            microphone = self.devices.find_microphone()
            command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                       "-thread_queue_size", "1024", "-f", "avfoundation", "-i", f":{device.index}"]
            if microphone and microphone.index != device.index:
                command += ["-thread_queue_size", "1024", "-f", "avfoundation", "-i", f":{microphone.index}"]
                self.source_paths.append(microphone_path)
                # Preserve each hardware clock in its own native-rate file. Live mixing
                # independent CoreAudio clocks can change pitch and corrupt speech.
                command += ["-map", "0:a", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", "-y", str(system_path),
                            "-map", "1:a", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", "-y", str(microphone_path)]
            else:
                command += ["-map", "0:a", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", "-y", str(system_path)]
            self.process = subprocess.Popen(command,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self._drain_stderr(self.process)
            time.sleep(.5)
            if self.process.poll() is not None:
                time.sleep(0.2)
                raise RecordingError(f"Unable to start recording: {self._recent_stderr()}")
            self.started_monotonic = time.monotonic()

    def stop(self) -> dict:
        with self._lock:
            if not self.process or self.started_monotonic is None:
                raise RecordingError("No recording is in progress.")
            if self.process.poll() is None:
                self.process.send_signal(2)
                try: self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.terminate(); self.process.wait(timeout=3)
            duration = time.monotonic() - self.started_monotonic
            saved_sources = [path for path in self.source_paths if path.exists() and path.stat().st_size > 0]
            if not saved_sources:
                raise RecordingError("Audio capture stopped before any recording data could be saved.")
            preview_error = None
            try:
                self._create_preview(duration)
            except Exception as exc:
                # The source tracks are the archival recordings. A preview failure
                # must never make a successfully captured meeting unrecoverable.
                preview_error = str(exc)
            measurable_path = self.path if self.path.exists() and self.path.stat().st_size > 0 else saved_sources[0]
            return {"path": str(self.path), "duration_seconds": round(duration, 2),
                    "capture_backend": self.capture_backend,
                    "size_bytes": sum(path.stat().st_size for path in saved_sources),
                    "source_paths": [str(path) for path in saved_sources],
                    "preview_error": preview_error,
                    **self.measure_audio(measurable_path)}

    def _create_preview(self, duration: float) -> None:
        """Create a convenient 16 kHz mix after capture; originals remain untouched."""
        if len(self.source_paths) == 1:
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(self.source_paths[0]),
                       "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(self.path)]
        else:
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(self.source_paths[0]), "-i", str(self.source_paths[1]),
                       "-filter_complex", "[0:a]aresample=16000:first_pts=0,volume=0.95[s];[1:a]aresample=16000:first_pts=0,highpass=f=90,lowpass=f=7500,volume=0.45[m];[s][m]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.90[a]",
                       "-map", "[a]", "-t", f"{duration:.3f}", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(self.path)]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=max(30, int(duration * 2)))
        if proc.returncode != 0:
            raise RecordingError(f"Recording was saved, but preview creation failed: {proc.stderr.strip()}")

    @staticmethod
    def measure_audio(path: Path) -> dict:
        """Measure captured signal so silence is never mistaken for a transcript."""
        try:
            proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                                  capture_output=True, text=True, timeout=30)
            mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
            peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", proc.stderr)
            mean_db = float(mean.group(1)) if mean else None
            peak_db = float(peak.group(1)) if peak else None
            return {"mean_volume_db": mean_db, "peak_volume_db": peak_db,
                    "has_audible_audio": peak_db is not None and peak_db > -60.0}
        except Exception:
            return {"mean_volume_db": None, "peak_volume_db": None, "has_audible_audio": None}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--test", action="store_true"); parser.add_argument("--seconds", type=int, default=5)
    args = parser.parse_args()
    if args.test:
        path = Path("data/meetings/audio-test.wav"); recorder = AudioRecorder(); recorder.start(path); time.sleep(args.seconds); print(recorder.stop())

if __name__ == "__main__": main()
