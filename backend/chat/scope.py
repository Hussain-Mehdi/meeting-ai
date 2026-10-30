"""Work out *which meetings* a question is about, before searching inside them.

Keyword search alone cannot answer "what did we decide in the last meeting": the words
"last" and "meeting" appear all over every transcript, so the match is noise. Questions
that name a meeting by time are resolved here against meeting dates instead, and the
retrieval is then scoped to those meetings.
"""
import re
from datetime import date, timedelta

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

MEETING_WORD = r"(?:meeting|call|sync|session|standup|stand-up|discussion|conversation|catch[- ]?up)"
LATEST_WORDS = r"(?:last|latest|most recent|recent|previous|final|newest)"


def detect_scope(question: str, today: date | None = None) -> dict:
    """Return which meetings a question refers to.

    {"kind": "latest", "count": n}  — the n most recent meetings
    {"kind": "range", "start": date, "end": date}  — meetings in an inclusive date range
    {"kind": "none"}  — the question is about content, not about a particular meeting
    """
    today = today or date.today()
    text = (question or "").casefold().strip()
    if not text:
        return {"kind": "none"}

    # "last week" / "this month" are ranges, and must be checked before "last ... meeting".
    if re.search(r"\blast week\b", text):
        monday = today - timedelta(days=today.weekday() + 7)
        return {"kind": "range", "start": monday, "end": monday + timedelta(days=6)}
    if re.search(r"\bthis week\b", text):
        monday = today - timedelta(days=today.weekday())
        return {"kind": "range", "start": monday, "end": today}
    if re.search(r"\blast month\b", text):
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return {"kind": "range", "start": last_prev.replace(day=1), "end": last_prev}
    if re.search(r"\bthis month\b", text):
        return {"kind": "range", "start": today.replace(day=1), "end": today}
    if re.search(r"\byesterday\b", text):
        return {"kind": "range", "start": today - timedelta(days=1), "end": today - timedelta(days=1)}
    if re.search(r"\btoday\b", text):
        return {"kind": "range", "start": today, "end": today}

    explicit = _explicit_date(text, today)
    if explicit:
        return {"kind": "range", "start": explicit, "end": explicit}

    # "the last N meetings" / "last meeting" / "what did we discuss last time"
    count = re.search(rf"\b{LATEST_WORDS}\s+(\d{{1,2}})\s+{MEETING_WORD}s?\b", text)
    if count:
        return {"kind": "latest", "count": max(1, min(int(count.group(1)), 10))}
    if re.search(rf"\b{LATEST_WORDS}\s+{MEETING_WORD}\b", text) or re.search(r"\blast time\b", text):
        return {"kind": "latest", "count": 1}
    if re.search(rf"\b{MEETING_WORD}\s+(?:was\s+)?(?:on\s+)?{LATEST_WORDS}\b", text):
        return {"kind": "latest", "count": 1}

    return {"kind": "none"}


def _explicit_date(text: str, today: date) -> date | None:
    """Parse "29 oct", "october 29", "29/10", "2026-10-29" — year optional, defaults sensibly."""
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    names = "|".join(MONTHS)
    day_first = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({names})\b(?:\s+(\d{{4}}))?", text)
    if day_first:
        year = int(day_first.group(3)) if day_first.group(3) else None
        return _with_year(int(day_first.group(1)), MONTHS[day_first.group(2)], year, today)

    month_first = re.search(rf"\b({names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", text)
    if month_first:
        year = int(month_first.group(3)) if month_first.group(3) else None
        return _with_year(int(month_first.group(2)), MONTHS[month_first.group(1)], year, today)

    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if numeric:                                   # day/month, the common form outside the US
        year = numeric.group(3)
        year = int(year) + 2000 if year and len(year) == 2 else (int(year) if year else None)
        return _with_year(int(numeric.group(1)), int(numeric.group(2)), year, today)
    return None


def _with_year(day: int, month: int, year: int | None, today: date) -> date | None:
    if year:
        return _safe_date(year, month, day)
    # No year given: assume the most recent occurrence, never the future.
    candidate = _safe_date(today.year, month, day)
    if candidate and candidate > today:
        return _safe_date(today.year - 1, month, day)
    return candidate


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
