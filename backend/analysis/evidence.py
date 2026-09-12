"""Map a verified evidence quote back to the transcript segment (and therefore the audio span) it came from."""
import re
from difflib import SequenceMatcher


def _canonical(text: str) -> str:
    return re.sub(r"[^\w\s]+", " ", str(text or "").casefold()).strip()


def locate_evidence(evidence: str, segments: list[dict]) -> dict | None:
    """Return {"segment_id", "start", "end", "speaker"} for the segment that best contains the quote.

    A quote can span two adjacent segments, so neighbouring pairs are checked too. Returns None
    when no segment covers the quote well enough; the caller then offers no audio link rather than
    a misleading one.
    """
    words = _canonical(evidence).split()
    if not words or not segments:
        return None
    quote = " ".join(words)

    def score(text: str) -> float:
        candidate = _canonical(text)
        if not candidate:
            return 0.0
        if quote in candidate:
            return 1.0
        matcher = SequenceMatcher(None, words, candidate.split(), autojunk=False)
        matched = sum(block.size for block in matcher.get_matching_blocks())
        return matched / len(words)

    def location(segment: dict, end_from: dict | None = None) -> dict:
        return {"segment_id": segment.get("id"), "start": segment.get("start"),
                "end": (end_from or segment).get("end"), "speaker": segment.get("speaker")}

    texts = [segment.get("text_en") or segment.get("text") or "" for segment in segments]
    # A quote fully inside one line always wins over a line pair that happens to also contain it.
    singles = [(score(text), index) for index, text in enumerate(texts)]
    best_value, best_index = max(singles, key=lambda item: item[0])
    if best_value >= 1.0:
        return location(segments[best_index])
    best: tuple[float, dict] = (best_value, location(segments[best_index]))
    for index in range(len(segments) - 1):
        value = score(f"{texts[index]} {texts[index + 1]}")
        if value > best[0]:
            best = (value, location(segments[index], end_from=segments[index + 1]))
    if best[0] >= 0.75:
        return best[1]
    return None


def attach_evidence_locations(items: list[dict], segments: list[dict], field: str = "evidence") -> None:
    for item in items:
        location = locate_evidence(item.get(field) or "", segments)
        item["evidence_location"] = location
