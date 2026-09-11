import asyncio
import logging
import subprocess
from dataclasses import dataclass


log = logging.getLogger(__name__)


@dataclass
class MeetTab:
    title: str
    url: str


class DetectionError(RuntimeError):
    """Chrome could not be inspected. This is not evidence that the meeting ended."""


class MeetDetector:
    # Every window and tab is wrapped in its own `try` so one odd window (Meet's
    # picture-in-picture pop-out, a tab picker, DevTools) cannot abort the whole scan.
    # The title is emitted after the URL on a single tab-separated line so an empty
    # title cannot collapse the output into a different shape.
    SCRIPT = '''tell application "Google Chrome"
if not running then return "NONE"
repeat with w in windows
try
repeat with t in tabs of w
try
set u to URL of t
if u starts with "https://meet.google.com/" then return "MEET" & tab & u & tab & (title of t)
end try
end repeat
end try
end repeat
return "NONE"
end tell'''

    def __init__(self, timeout: float = 15.0, end_confirmations: int = 5):
        self.timeout = timeout
        self.end_confirmations = max(1, int(end_confirmations))

    def detect(self) -> MeetTab | None:
        """Return the open Meet tab, None when Chrome has no Meet tab, or raise DetectionError."""
        try:
            proc = subprocess.run(["osascript", "-e", self.SCRIPT], capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise DetectionError(f"Chrome did not answer within {self.timeout:.0f}s")
        except OSError as exc:
            raise DetectionError(f"osascript could not run: {exc}")
        if proc.returncode != 0:
            raise DetectionError(f"osascript exited with {proc.returncode}: {proc.stderr.strip()[:200]}")
        output = proc.stdout.rstrip("\n")
        if output == "NONE":
            return None
        if output.startswith("MEET\t"):
            parts = output.split("\t", 2)
            url = parts[1].strip()
            title = parts[2].strip() if len(parts) > 2 else ""
            if url:
                return MeetTab(title or "Google Meet", url)
        raise DetectionError(f"unexpected osascript output: {output[:200]!r}")

    async def monitor(self, on_detected, on_ended, interval=3):
        previous = None
        misses = 0
        failures = 0
        while True:
            try:
                current = await asyncio.to_thread(self.detect)
            except DetectionError as exc:
                # A failed reading is not a closed tab: keep the current belief and never
                # end a meeting because of it. Log the first failure and then every 10th.
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    log.warning("meet detection failed (%s in a row): %s", failures, exc)
                await asyncio.sleep(interval)
                continue
            if failures:
                log.info("meet detection recovered after %s failed readings", failures)
                failures = 0
            if current:
                misses = 0
                if not previous or current.url != previous.url:
                    await on_detected(current)
                previous = current
            elif previous:
                misses += 1
                if misses >= self.end_confirmations:
                    log.info("meet tab gone for %s consecutive checks, treating meeting as ended url=%s", misses, previous.url)
                    await on_ended(previous)
                    previous = None
                    misses = 0
            await asyncio.sleep(interval)
