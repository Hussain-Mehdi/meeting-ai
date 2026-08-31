import re
from datetime import date, timedelta


WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}


def normalize_deadline(text: str, meeting_date: date) -> str | None:
    value = text.lower().strip()
    if "tomorrow" in value:
        return (meeting_date + timedelta(days=1)).isoformat()
    for name, weekday in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", value):
            delta = (weekday - meeting_date.weekday()) % 7
            if delta == 0:
                delta = 7
            return (meeting_date + timedelta(days=delta)).isoformat()
    return None

