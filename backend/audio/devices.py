import json
import platform
import re
import subprocess
from dataclasses import dataclass, asdict


@dataclass
class AudioDevice:
    index: str
    name: str
    input: bool = True


class AudioDeviceManager:
    def _ffmpeg_output(self) -> str:
        if platform.system() != "Darwin": return ""
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                              capture_output=True, text=True, timeout=10)
        return proc.stderr

    def list_devices(self) -> list[AudioDevice]:
        output = self._ffmpeg_output()
        audio = output.split("AVFoundation audio devices:", 1)[-1] if "AVFoundation audio devices:" in output else ""
        if "AVFoundation video devices:" in audio:
            audio = audio.split("AVFoundation video devices:", 1)[0]
        result = []
        for index, name in re.findall(r"\[(\d+)\]\s+(.+)", audio):
            result.append(AudioDevice(index=index, name=name.strip()))
        return result

    def find_blackhole(self) -> AudioDevice | None:
        return next((d for d in self.list_devices() if "blackhole" in d.name.lower()), None)

    def find_microphone(self) -> AudioDevice | None:
        devices = self.list_devices()
        preferred = next((d for d in devices if "macbook" in d.name.lower() and "microphone" in d.name.lower()), None)
        return preferred or next((d for d in devices if "blackhole" not in d.name.lower()), None)

    def get_default_input(self):
        return self._system_default("input")

    def get_default_output(self):
        return self._system_default("output")

    def _system_default(self, kind):
        script = f'get volume settings' if kind == "output" else 'input volume of (get volume settings)'
        try:
            return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3).stdout.strip() or None
        except Exception: return None

    def report(self):
        devices = [asdict(x) for x in self.list_devices()]
        blackhole = next((d for d in devices if "blackhole" in d["name"].lower()), None)
        microphone = next((d for d in devices if "macbook" in d["name"].lower() and "microphone" in d["name"].lower()), None)
        return {"devices": devices, "blackhole": blackhole,
                "microphone": microphone,
                "connected": bool(blackhole), "message": "BlackHole 2ch detected." if blackhole else "BlackHole 2ch was not detected."}


if __name__ == "__main__":
    print(json.dumps(AudioDeviceManager().report(), indent=2))
