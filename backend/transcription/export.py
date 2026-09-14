"""Plain-text / SRT / JSON renderings of a saved transcript for copying and downloading."""
import json
from datetime import datetime


def _clock(seconds: float) -> str:
    total = int(max(0.0, float(seconds or 0)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _srt_time(seconds: float) -> str:
    total = max(0.0, float(seconds or 0))
    hours, rest = divmod(int(total), 3600)
    minutes, secs = divmod(rest, 60)
    millis = int(round((total - int(total)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _line_text(segment: dict, language: str) -> str:
    original = str(segment.get("text") or "").strip()
    english = str(segment.get("text_en") or "").strip()
    if language == "english":
        return english or original
    if language == "both" and english and english != original:
        return f"{original}\n    ({english})"
    return original


def transcript_as_text(meeting: dict, segments: list[dict], language: str = "both", timestamps: bool = True) -> str:
    """One line per utterance: `[m:ss] Speaker: text`. With language="both", a differing English
    rendering follows the original on an indented line. The original text is never altered."""
    started = meeting.get("started_at") or ""
    try:
        when = datetime.fromisoformat(started).strftime("%d %B %Y, %H:%M")
    except ValueError:
        when = started
    header = [meeting.get("title") or "Meeting", when, f"Duration: {round((meeting.get('duration_seconds') or 0) / 60)} minutes",
              "Transcript generated locally by Meeting AI", ""]
    lines = []
    for segment in segments:
        text = _line_text(segment, language)
        if not text:
            continue
        prefix = f"[{_clock(segment.get('start'))}] " if timestamps else ""
        lines.append(f"{prefix}{segment.get('speaker') or 'Speaker'}: {text}")
    return "\n".join(header + lines).rstrip() + "\n"


def transcript_as_srt(segments: list[dict], language: str = "original") -> str:
    blocks = []
    for number, segment in enumerate((s for s in segments if str(s.get("text") or "").strip()), start=1):
        text = _line_text(segment, "english" if language == "english" else "original")
        blocks.append(f"{number}\n{_srt_time(segment.get('start'))} --> {_srt_time(segment.get('end'))}\n{segment.get('speaker') or 'Speaker'}: {text}\n")
    return "\n".join(blocks)


def transcript_as_json(meeting: dict, segments: list[dict], speakers: dict | None = None) -> str:
    payload = {
        "meeting": {"id": meeting.get("id"), "title": meeting.get("title"), "started_at": meeting.get("started_at"),
                    "ended_at": meeting.get("ended_at"), "duration_seconds": meeting.get("duration_seconds"),
                    "platform": meeting.get("platform"), "template": meeting.get("template")},
        "speakers": {key: {k: v for k, v in info.items() if k != "centroid"} for key, info in (speakers or {}).items()},
        "segments": segments,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def transcript_filename(title: str, started_at: str, extension: str) -> str:
    from backend.reports.pdf import report_filename
    base = report_filename(title or "Meeting", started_at or "")
    return base.replace("-report.pdf", f"-transcript.{extension}")
