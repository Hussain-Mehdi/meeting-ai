import asyncio
import subprocess
from dataclasses import dataclass


@dataclass
class MeetTab:
    title: str
    url: str


class MeetDetector:
    SCRIPT = '''tell application "Google Chrome"
if not running then return ""
repeat with w in windows
repeat with t in tabs of w
set u to URL of t
if u starts with "https://meet.google.com/" then return (title of t) & linefeed & u
end repeat
end repeat
return ""
end tell'''

    def detect(self):
        try:
            proc = subprocess.run(["osascript", "-e", self.SCRIPT], capture_output=True, text=True, timeout=5)
            lines = proc.stdout.strip().splitlines()
            return MeetTab(lines[0], lines[1]) if len(lines) >= 2 else None
        except Exception: return None

    async def monitor(self, on_detected, on_ended, interval=3):
        previous = None
        while True:
            current = await asyncio.to_thread(self.detect)
            if current and (not previous or current.url != previous.url): await on_detected(current)
            if previous and not current: await on_ended(previous)
            previous = current
            await asyncio.sleep(interval)

