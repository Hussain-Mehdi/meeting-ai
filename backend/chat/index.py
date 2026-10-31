"""Full-text search over everything a meeting produced, so chat answers can cite real evidence.

SQLite FTS5 keeps the whole index inside the existing local database: no extra service, no
model, nothing to keep warm. Transcript lines are grouped into short passages so a retrieved
hit carries enough conversation to be meaningful on its own.
"""
import logging
import re

log = logging.getLogger(__name__)

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chat_index USING fts5(
  meeting_id UNINDEXED, kind UNINDEXED, ref UNINDEXED, heading, content,
  start UNINDEXED, end UNINDEXED, tokenize='porter unicode61'
);
"""

# Grouping a few consecutive lines keeps a retrieved passage readable without making it so
# long that one hit swamps the model's context.
PASSAGE_LINES = 8
PASSAGE_OVERLAP = 2

STOPWORDS = {
    "a", "about", "all", "also", "am", "an", "and", "any", "are", "as", "at", "be", "been", "but",
    "by", "can", "did", "do", "does", "for", "from", "get", "had", "has", "have", "how", "i", "if",
    "in", "is", "it", "its", "just", "me", "my", "of", "on", "or", "our", "should", "so", "than",
    "that", "the", "their", "them", "then", "there", "these", "they", "this", "to", "up", "us",
    "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "would",
    "you", "your", "tell", "give", "show", "say", "said", "need", "want", "know", "think", "going",
}


def build_query(question: str) -> str:
    """Turn a natural question into an FTS5 OR query over its meaningful words.

    Stopwords are dropped so "what should we do about predictions" ranks on "predictions"
    rather than on "what" and "should". Returns "" when nothing useful is left.
    """
    words = re.findall(r"[^\W_]+", (question or "").casefold(), flags=re.UNICODE)
    terms = [w for w in words if len(w) > 2 and w not in STOPWORDS]
    if not terms:
        terms = [w for w in words if len(w) > 2]
    if not terms:
        return ""
    unique, seen = [], set()
    for term in terms:
        if term not in seen:
            seen.add(term)
            unique.append(term)
    return " OR ".join(f'"{term}"*' for term in unique[:12])


def _passages(segments: list[dict]) -> list[dict]:
    """Group transcript lines into overlapping passages, keeping speakers and timings."""
    values = []
    # `ref` identifies a row for the whole lifetime of the index, including the vector table
    # keyed on it, so the fallback carries the window position and can never collide.
    step = max(1, PASSAGE_LINES - PASSAGE_OVERLAP)
    for start in range(0, len(segments), step):
        window = segments[start:start + PASSAGE_LINES]
        if not window:
            break
        lines = [f"{s.get('speaker') or 'Speaker'}: {str(s.get('text_en') or s.get('text') or '').strip()}"
                 for s in window if str(s.get('text_en') or s.get('text') or '').strip()]
        if lines:
            speakers = sorted({str(s.get("speaker")) for s in window if s.get("speaker")})
            values.append({
                "kind": "transcript",
                "ref": str(window[0].get("id") or f"w{start}"),
                "heading": ", ".join(speakers)[:120],
                "content": "\n".join(lines),
                "start": float(window[0].get("start") or 0),
                "end": float(window[-1].get("end") or 0),
            })
        if start + PASSAGE_LINES >= len(segments):
            break
    return values


def meeting_rows(meeting: dict, segments: list[dict]) -> list[dict]:
    """Every indexable passage for one meeting: its analysis claims and its transcript.

    Each row's `ref` must be unique within the meeting: it is half the identity that the
    chat index and the vector table share, so two rows sharing one would silently collapse
    into a single vector. Items without an id fall back to their ordinal, not their text.
    """
    rows: list[dict] = []
    ordinal = 0

    def unique(value, prefix: str) -> str:
        nonlocal ordinal
        ordinal += 1
        return str(value) if value else f"{prefix}{ordinal}"

    if meeting.get("summary"):
        rows.append({"kind": "summary", "ref": "summary", "heading": "Summary",
                     "content": meeting["summary"], "start": 0.0, "end": 0.0})
    for table, heading in (("goals", "Goal"), ("key_topics", "Key topic"),
                           ("decisions", "Decision"), ("next_steps", "Next step")):
        for item in meeting.get(table) or []:
            value = str(item.get("value") or "").strip()
            if value:
                rows.append({"kind": table, "ref": unique(item.get("id"), f"{table}-"), "heading": heading,
                             "content": value, "start": 0.0, "end": 0.0})
    for task in meeting.get("tasks") or []:
        text = str(task.get("task") or "").strip()
        if not text:
            continue
        parts = [text]
        if task.get("owner"): parts.append(f"Owner: {task['owner']}")
        if task.get("deadline_original"): parts.append(f"Due: {task['deadline_original']}")
        if task.get("evidence"): parts.append(f"Evidence: {task['evidence']}")
        rows.append({"kind": "task", "ref": unique(task.get("id"), "task-"), "heading": "Task",
                     "content": "\n".join(parts), "start": 0.0, "end": 0.0})
    for change in meeting.get("requested_changes") or []:
        text = str(change.get("change_text") or change.get("change") or "").strip()
        if not text:
            continue
        parts = [text]
        if change.get("requested_of"): parts.append(f"Requested of: {change['requested_of']}")
        if change.get("evidence"): parts.append(f"Evidence: {change['evidence']}")
        rows.append({"kind": "requested_change", "ref": unique(change.get("id"), "change-"),
                     "heading": "Requested change", "content": "\n".join(parts), "start": 0.0, "end": 0.0})
    for person in meeting.get("people") or []:
        name = str(person.get("name") or "").strip()
        if name:
            rows.append({"kind": "person", "ref": unique(person.get("id"), "person-"),
                         "heading": f"Person mentioned: {name}",
                         "content": f"{name}. {person.get('context') or ''}".strip(), "start": 0.0, "end": 0.0})
    rows.extend(_passages(segments))
    return rows
