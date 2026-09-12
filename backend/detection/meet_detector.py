import asyncio
import logging
import subprocess
from dataclasses import dataclass


log = logging.getLogger(__name__)


@dataclass
class MeetTab:
    title: str
    url: str
    platform: str = "google_meet"


class DetectionError(RuntimeError):
    """A provider could not be inspected. This is not evidence that the meeting ended."""


def _osascript(script: str, timeout: float) -> str:
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DetectionError(f"osascript did not answer within {timeout:.0f}s")
    except OSError as exc:
        raise DetectionError(f"osascript could not run: {exc}")
    if proc.returncode != 0:
        raise DetectionError(f"osascript exited with {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout.rstrip("\n")


class BrowserTabProvider:
    """Finds meeting tabs in Chromium-family browsers (Chrome, Brave, Edge, Arc share the same AppleScript dictionary).

    Every window and tab is wrapped in its own `try` so one odd window (a picture-in-picture
    pop-out, a tab picker, DevTools) cannot abort the whole scan. The title is emitted after
    the URL on a single tab-separated line so an empty title cannot change the output shape.
    """

    PATTERNS = {
        "google_meet": ("https://meet.google.com/",),
        "zoom": ("https://zoom.us/j/", "https://zoom.us/wc/", "https://app.zoom.us/wc/", ".zoom.us/j/", ".zoom.us/wc/"),
        "teams": ("https://teams.microsoft.com/", "https://teams.live.com/", "https://teams.cloud.microsoft/"),
    }

    def __init__(self, app_name: str = "Google Chrome", platforms=None):
        self.app_name = app_name
        self.platforms = {key: self.PATTERNS[key] for key in (platforms or self.PATTERNS)}
        conditions = " or ".join(
            f'u starts with "{pattern}"' if pattern.startswith("http") else f'u contains "{pattern}"'
            for patterns in self.platforms.values() for pattern in patterns
        )
        # `tab` inside a Chrome tell-block resolves to Chrome's tab *class*, so the
        # delimiter is built explicitly outside the block.
        self.script = f'''set d to (ASCII character 9)
tell application "{app_name}"
if not running then return "NONE"
repeat with w in windows
try
repeat with t in tabs of w
try
set u to URL of t
if {conditions} then return "MEET" & d & u & d & (title of t)
end try
end repeat
end try
end repeat
return "NONE"
end tell'''

    def platform_for(self, url: str) -> str | None:
        for key, patterns in self.platforms.items():
            for pattern in patterns:
                if (pattern.startswith("http") and url.startswith(pattern)) or (not pattern.startswith("http") and pattern in url):
                    return key
        return None

    def detect(self, timeout: float) -> MeetTab | None:
        output = _osascript(self.script, timeout)
        if output == "NONE":
            return None
        if output.startswith("MEET\t"):
            parts = output.split("\t", 2)
            url = parts[1].strip()
            title = parts[2].strip() if len(parts) > 2 else ""
            platform = self.platform_for(url)
            if url and platform:
                return MeetTab(title or platform.replace("_", " ").title(), url, platform)
        raise DetectionError(f"unexpected osascript output from {self.app_name}: {output[:200]!r}")


class ZoomAppProvider:
    """The Zoom desktop app spawns a `CptHost` helper only while a meeting is in progress."""

    platform = "zoom"

    def detect(self, timeout: float) -> MeetTab | None:
        try:
            proc = subprocess.run(["pgrep", "-x", "CptHost"], capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DetectionError(f"pgrep failed: {exc}")
        if proc.returncode == 0 and proc.stdout.strip():
            return MeetTab("Zoom meeting", "zoom://meeting", "zoom")
        return None


class TeamsAppProvider:
    """The Teams desktop app opens a separate meeting window whose name starts with the call subject
    and contains "| Microsoft Teams" while a call is active. Needs Accessibility access for System Events;
    without it the provider raises DetectionError and is simply ignored."""

    platform = "teams"
    SCRIPT = '''set d to (ASCII character 9)
tell application "System Events"
if not (exists process "Microsoft Teams") then return "NONE"
try
set names to name of windows of process "Microsoft Teams"
repeat with n in names
set s to n as string
if s contains "| Microsoft Teams" and (s contains "Meeting" or s contains "Call" or s contains "meeting" or s contains "call") then return "MEET" & d & s
end repeat
end try
return "NONE"
end tell'''

    def detect(self, timeout: float) -> MeetTab | None:
        # System Events is slow to answer; skip it entirely unless Teams is running.
        try:
            running = subprocess.run(["pgrep", "-f", "Microsoft Teams"], capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DetectionError(f"pgrep failed: {exc}")
        if running.returncode != 0 or not running.stdout.strip():
            return None
        output = _osascript(self.SCRIPT, timeout)
        if output.startswith("MEET\t"):
            title = output.split("\t", 1)[1].strip()
            return MeetTab(title or "Teams meeting", "teams://meeting", "teams")
        return None


class MeetDetector:
    """Polls every provider; the first provider that reports a meeting wins.

    A provider error is never treated as "no meeting": the reading from the other providers
    is used, and if none can answer the previous belief is kept.
    """

    def __init__(self, timeout: float = 15.0, end_confirmations: int = 5, providers=None,
                 browsers=("Google Chrome",), zoom_app: bool = True, teams_app: bool = True):
        self.timeout = timeout
        self.end_confirmations = max(1, int(end_confirmations))
        if providers is None:
            providers = [BrowserTabProvider(app) for app in browsers]
            if zoom_app: providers.append(ZoomAppProvider())
            if teams_app: providers.append(TeamsAppProvider())
        self.providers = providers

    @property
    def SCRIPT(self) -> str:  # kept for older callers and tests
        first = next((p for p in self.providers if isinstance(p, BrowserTabProvider)), None)
        return first.script if first else ""

    def detect(self) -> MeetTab | None:
        """Return the active meeting, None when no provider sees one, or raise DetectionError if
        every provider failed (so nothing at all could be observed)."""
        errors = []
        for provider in self.providers:
            try:
                found = provider.detect(self.timeout)
            except DetectionError as exc:
                errors.append(f"{type(provider).__name__}: {exc}")
                continue
            if found:
                return found
        if errors and len(errors) == len(self.providers):
            raise DetectionError("; ".join(errors))
        if errors:
            log.debug("some detection providers failed: %s", "; ".join(errors))
        return None

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
                    log.warning("meeting detection failed (%s in a row): %s", failures, exc)
                await asyncio.sleep(interval)
                continue
            if failures:
                log.info("meeting detection recovered after %s failed readings", failures)
                failures = 0
            if current:
                misses = 0
                if not previous or current.url != previous.url:
                    await on_detected(current)
                previous = current
            elif previous:
                misses += 1
                if misses >= self.end_confirmations:
                    log.info("meeting gone for %s consecutive checks, treating it as ended platform=%s url=%s",
                             misses, previous.platform, previous.url)
                    await on_ended(previous)
                    previous = None
                    misses = 0
            await asyncio.sleep(interval)
