import json
import re
from datetime import date
from typing import Any

import httpx

from backend.analysis.dates import normalize_deadline
from backend.analysis.prompts import (
    SYSTEM_PROMPT,
    CHUNK_PROMPT,
    FINAL_PROMPT,
)
from backend.analysis.schemas import MeetingAnalysis


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------------
# Chunk extraction schema
# ---------------------------------------------------------------------

CHUNK_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_points": {
            "type": "array",
            "items": {"type": "string"},
        },
        "goals": {
            "type": "array",
            "items": {"type": "string"},
        },
        "key_topics": {
            "type": "array",
            "items": {"type": "string"},
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "evidence": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": [
                    "decision",
                    "confidence",
                    "evidence",
                    "status",
                ],
            },
        },
        "unresolved_items": {
            "type": "array",
            "items": {"type": "string"},
        },
        "people_mentioned": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "context": {"type": "string"},
                    "importance": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "name",
                    "context",
                    "importance",
                ],
            },
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "action": {"type": "string"},
                    "task": {"type": "string"},
                    "deadline": {
                        "anyOf": [
                            {"type": "null"},
                            {"type": "string"},
                        ]
                    },
                    "original_deadline_phrase": {
                        "anyOf": [
                            {"type": "null"},
                            {"type": "string"},
                        ]
                    },
                    "priority": {"type": "string"},
                    "status": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                    "source_chunk_id": {"type": "string"},
                },
                "required": [
                    "owner",
                    "action",
                    "task",
                    "priority",
                    "status",
                    "confidence",
                    "evidence",
                    "source_chunk_id",
                ],
            },
        },
        "next_steps": {
            "type": "array",
            "items": {"type": "string"},
        },
        "important_entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "context": {"type": "string"},
                },
                "required": [
                    "name",
                    "type",
                    "context",
                ],
            },
        },
    },
    "required": [
        "summary_points",
        "goals",
        "key_topics",
        "decisions",
        "unresolved_items",
        "people_mentioned",
        "tasks",
        "next_steps",
        "important_entities",
    ],
}


# ---------------------------------------------------------------------
# Transcript chunking
# ---------------------------------------------------------------------

def chunk_transcript(
    text: str,
    size: int = 12_000,
    overlap_lines: int = 6,
) -> list[str]:
    """
    Split the transcript primarily at line boundaries.

    Meeting transcripts often contain speaker-separated lines. Preserving whole
    lines is safer than splitting at arbitrary character positions because a
    task/decision may depend on one complete utterance.

    A small line overlap helps preserve cross-boundary conversational context.
    """

    text = (text or "").strip()

    if not text:
        return []

    lines = text.splitlines()

    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    for line in lines:
        line_size = len(line) + 1

        # Extremely long single line fallback.
        if line_size > size:
            if current:
                chunks.append("\n".join(current).strip())
                current = []
                current_size = 0

            start = 0

            while start < len(line):
                end = min(start + size, len(line))

                # Prefer sentence-ish boundary.
                if end < len(line):
                    candidates = [
                        line.rfind(". ", start, end),
                        line.rfind("? ", start, end),
                        line.rfind("! ", start, end),
                    ]
                    boundary = max(candidates)

                    if boundary > start + size // 2:
                        end = boundary + 1

                chunks.append(line[start:end].strip())

                if end >= len(line):
                    break

                start = end

            continue

        if current and current_size + line_size > size:
            chunks.append("\n".join(current).strip())

            overlap = current[-overlap_lines:] if overlap_lines else []

            current = list(overlap)
            current_size = sum(len(x) + 1 for x in current)

        current.append(line)
        current_size += line_size

    if current:
        chunk = "\n".join(current).strip()

        if not chunks or chunk != chunks[-1]:
            chunks.append(chunk)

    return chunks


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _canonical_text(value: str) -> str:
    """
    Used only for deterministic duplicate detection.
    It must never replace the user-visible/source wording.
    """

    value = str(value or "").casefold()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\s]", "", value)
    return value.strip()


def _find_source_quote(
    quote: str,
    source: str,
) -> str | None:
    """
    Verify that evidence actually exists in the source.

    First checks exact text. Then allows whitespace differences caused by
    transcript formatting while returning the actual source text rather than
    invented/normalized evidence.
    """

    quote = str(quote or "").strip()

    if not quote:
        return None

    if quote in source:
        return quote

    # Avoid pathological regexes.
    if len(quote) > 2_000:
        return None

    tokens = quote.split()

    if not tokens:
        return None

    pattern = r"\s+".join(re.escape(token) for token in tokens)

    match = re.search(
        pattern,
        source,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(0)


def _name_appears_in_source(
    name: str,
    transcript: str,
) -> bool:
    name = str(name or "").strip()

    if not name:
        return False

    pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"

    return bool(
        re.search(
            pattern,
            transcript,
            flags=re.IGNORECASE,
        )
    )


def _categorical_level(value: Any, default: str = "low") -> str:
    """Normalize model confidence/priority variants to the report schema."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        score = max(0.0, min(1.0, float(value)))
        return "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
    text = str(value or "").strip().casefold()
    if text in {"high", "medium", "low"}:
        return text
    try:
        score = max(0.0, min(1.0, float(text)))
        return "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
    except (TypeError, ValueError):
        pass
    if text in {"urgent", "critical", "highest"}:
        return "high"
    if text in {"normal", "standard", "moderate"}:
        return "medium"
    if text in {"optional", "minor", "lowest"}:
        return "low"
    return default


def _normalize_string_list(value: Any, keys: tuple[str, ...]) -> list[str]:
    """Repair common object/list variants without creating new claims."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            item = next((item.get(key) for key in keys if item.get(key)), "")
        text = str(item or "").strip()
        canonical = _canonical_text(text)
        if text and canonical not in seen:
            seen.add(canonical)
            normalized.append(text)
    return normalized


# ---------------------------------------------------------------------
# LLM service
# ---------------------------------------------------------------------

class LLMService:
    def __init__(
        self,
        host: str,
        model: str,
        user_name: str,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.user_name = user_name

        self.client = httpx.Client(
            timeout=httpx.Timeout(
                connect=5,
                read=1800,
                write=30,
                pool=5,
            )
        )

    def availability(self) -> dict:
        try:
            response = self.client.get(
                f"{self.host}/api/tags",
            )
            response.raise_for_status()

            payload = response.json()

            names = [
                model.get("name", "")
                for model in payload.get("models", [])
                if isinstance(model, dict)
            ]

            configured_base = self.model.split(":")[0]

            installed = (
                self.model in names
                or any(
                    name.split(":")[0] == configured_base
                    for name in names
                )
            )

            return {
                "available": True,
                "model_installed": installed,
                "model": self.model,
                "message": (
                    None
                    if installed
                    else f"Ollama is running, but model '{self.model}' is not installed."
                ),
            }

        except httpx.ConnectError:
            return {
                "available": False,
                "model_installed": False,
                "model": self.model,
                "message": "Unable to connect to Ollama.",
            }

        except httpx.TimeoutException:
            return {
                "available": False,
                "model_installed": False,
                "model": self.model,
                "message": "Ollama did not respond in time.",
            }

        except Exception as exc:
            return {
                "available": False,
                "model_installed": False,
                "model": self.model,
                "message": f"Unable to verify Ollama availability: {exc}",
            }

    def _system_prompt(
        self,
        meeting_date: str | None = None,
    ) -> str:
        """
        Supports both the original system prompt and the refined prompt that
        contains {meeting_date}.
        """

        values = {
            "user_name": self.user_name,
            "meeting_date": meeting_date or "unknown",
        }

        return SYSTEM_PROMPT.format(**values)

    def _generate_json(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        meeting_date: str | None = None,
    ) -> dict:
        try:
            response = self.client.post(
                f"{self.host}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "system": self._system_prompt(meeting_date),
                    "stream": False,
                    "format": schema or "json",
                    "think": False,
                    "options": {
                        "temperature": 0,
                    },
                },
            )

            response.raise_for_status()

        except httpx.TimeoutException as exc:
            raise LLMError(
                "Ollama timed out while generating meeting analysis."
            ) from exc

        except httpx.HTTPError as exc:
            raise LLMError(
                f"Ollama request failed: {exc}"
            ) from exc

        try:
            payload = response.json()
            raw = str(payload["response"]).strip()

        except Exception as exc:
            raise LLMError(
                "Ollama returned an invalid API response."
            ) from exc

        # Normally unnecessary with Ollama structured output, but harmless
        # protection for models that occasionally wrap JSON in code fences.
        raw = re.sub(
            r"^\s*```(?:json)?\s*",
            "",
            raw,
            flags=re.IGNORECASE,
        )

        raw = re.sub(
            r"\s*```\s*$",
            "",
            raw,
        )

        try:
            parsed = json.loads(raw)

        except json.JSONDecodeError as exc:
            raise LLMError(
                f"Ollama returned malformed JSON: {exc}"
            ) from exc

        if not isinstance(parsed, dict):
            raise LLMError(
                "Ollama returned JSON, but the root value was not an object."
            )

        return parsed

    # -----------------------------------------------------------------
    # Main analysis
    # -----------------------------------------------------------------

    def analyze(
        self,
        transcript: str,
        metadata: dict,
        progress_callback=None,
    ) -> MeetingAnalysis:

        transcript = (transcript or "").strip()

        if not transcript:
            raise LLMError(
                "Transcript is empty. Analysis was skipped to prevent hallucinations."
            )

        if not metadata.get("date"):
            raise LLMError(
                "Meeting date is required for reliable deadline normalization."
            )

        try:
            meeting_day = date.fromisoformat(metadata["date"])

        except (TypeError, ValueError) as exc:
            raise LLMError(
                "Meeting metadata contains an invalid ISO meeting date."
            ) from exc

        status = self.availability()

        if not status["available"] or not status["model_installed"]:
            raise LLMError(
                status["message"] or "Ollama is unavailable."
            )

        chunks = chunk_transcript(transcript)

        if not chunks:
            raise LLMError(
                "Transcript contained no analyzable content."
            )

        # -------------------------------------------------------------
        # Stage 1: evidence extraction
        # -------------------------------------------------------------

        candidates: list[dict[str, Any]] = []

        for index, chunk in enumerate(chunks):
            chunk_id = f"chunk_{index + 1:03d}"

            if progress_callback:
                progress_callback(
                    68 + round(
                        12 * index / max(1, len(chunks))
                    ),
                    f"Analyzing transcript part {index + 1} of {len(chunks)}",
                )

            chunk_prompt = CHUNK_PROMPT.format(
                transcript=chunk,
                chunk_id=chunk_id,
                user_name=self.user_name,
            )

            extracted = self._generate_json(
                chunk_prompt,
                schema=CHUNK_JSON_SCHEMA,
                meeting_date=metadata["date"],
            )

            # Never trust a model-generated chunk ID.
            for task in extracted.get("tasks", []):
                if isinstance(task, dict):
                    task["source_chunk_id"] = chunk_id

            candidates.append(
                {
                    "chunk_id": chunk_id,
                    "evidence": extracted,
                }
            )

        # -------------------------------------------------------------
        # Stage 2: final synthesis
        # -------------------------------------------------------------

        if progress_callback:
            progress_callback(
                82,
                "Building the high-level meeting summary",
            )

        # Supplying the transcript for a short meeting allows the final model
        # to re-check chunk extraction.
        #
        # For longer transcripts the evidence candidates become authoritative.
        source_transcript = (
            transcript
            if len(transcript) <= 16_000
            else None
        )

        schema = MeetingAnalysis.model_json_schema()

        prompt = FINAL_PROMPT.format(
            metadata=json.dumps(
                metadata,
                ensure_ascii=False,
            ),
            schema=json.dumps(
                schema,
                ensure_ascii=False,
            ),
            candidates=json.dumps(
                candidates,
                ensure_ascii=False,
            ),
            source_transcript=json.dumps(
                source_transcript,
                ensure_ascii=False,
            ),
            meeting_date=metadata["date"],
        )

        last_error: Exception | None = None

        for attempt in range(2):
            try:
                generated = self._generate_json(
                    prompt,
                    schema=schema,
                    meeting_date=metadata["date"],
                )

                sanitized = self._sanitize(
                    generated,
                    transcript=transcript,
                    metadata=metadata,
                )

                analysis = MeetingAnalysis.model_validate(
                    sanitized
                )

                break

            except Exception as exc:
                last_error = exc

                if attempt == 0:
                    prompt += (
                        "\n\nVALIDATION FAILURE\n"
                        "Your previous JSON did not satisfy the required schema "
                        "or evidence constraints.\n"
                        f"Validation error: {exc}\n\n"
                        "Re-check the source evidence and return the COMPLETE "
                        "corrected JSON object. Do not add explanations."
                    )

        else:
            raise LLMError(
                f"Ollama returned invalid meeting JSON: {last_error}"
            )

        # -------------------------------------------------------------
        # Deterministic authoritative fields
        # -------------------------------------------------------------

        # Metadata should never be decided by the model.
        analysis.meeting = type(analysis.meeting).model_validate(
            metadata
        )

        # Browser attendee metadata is not currently available.
        analysis.attendees = []

        if progress_callback:
            progress_callback(
                91,
                "Checking tasks, people, dates, and evidence",
            )

        # -------------------------------------------------------------
        # Deterministic deadline normalization
        # -------------------------------------------------------------

        for task in analysis.tasks:
            deadline = getattr(task, "deadline", None)

            if not deadline:
                continue

            original = getattr(
                deadline,
                "original",
                None,
            )

            normalized = getattr(
                deadline,
                "normalized",
                None,
            )

            if original and not normalized:
                deadline.normalized = normalize_deadline(
                    original,
                    meeting_day,
                )

        return analysis

    # -----------------------------------------------------------------
    # Evidence validation
    # -----------------------------------------------------------------

    def _sanitize(
        self,
        raw: dict,
        transcript: str,
        metadata: dict,
    ) -> dict:
        """
        Deterministic safety pass.

        Important principle:
        this function VALIDATES and REMOVES unsupported model output.

        It must not invent:
        - evidence
        - tasks
        - owners
        - deadlines
        - people
        - decisions
        """

        if not isinstance(raw, dict):
            return {}

        raw = dict(raw)

        # Metadata is authoritative.
        raw["meeting"] = metadata

        # Attendees cannot currently be inferred.
        raw["attendees"] = []

        summary = raw.get("summary", "")
        if isinstance(summary, list):
            summary = " ".join(str(item).strip() for item in summary if str(item).strip())
        elif isinstance(summary, dict):
            summary = summary.get("summary") or summary.get("text") or ""
        raw["summary"] = str(summary or "").strip()

        raw["goals"] = _normalize_string_list(raw.get("goals", []), ("goal", "value", "text"))
        raw["key_topics"] = _normalize_string_list(raw.get("key_topics", []), ("topic", "value", "text"))
        raw["decisions"] = _normalize_string_list(raw.get("decisions", []), ("decision", "value", "text"))
        raw["next_steps"] = _normalize_string_list(raw.get("next_steps", []), ("step", "value", "text"))

        # -------------------------------------------------------------
        # Validate tasks
        # -------------------------------------------------------------

        valid_tasks: list[dict] = []
        seen_tasks: set[tuple] = set()

        task_values = raw.get("tasks", [])

        if not isinstance(task_values, list):
            task_values = []

        for task in task_values:
            if not isinstance(task, dict):
                continue

            owner = str(
                task.get("owner") or ""
            ).strip()

            task_text = str(
                task.get("task") or task.get("action") or ""
            ).strip()

            evidence = str(
                task.get("evidence") or ""
            ).strip()

            if not owner or not task_text or not evidence:
                continue

            task["owner"] = owner
            task["task"] = task_text

            # Evidence MUST exist in the transcript.
            source_evidence = _find_source_quote(
                evidence,
                transcript,
            )

            if not source_evidence:
                continue

            task["evidence"] = source_evidence

            # ---------------------------------------------------------
            # Validate owner representation
            # ---------------------------------------------------------

            owner_key = owner.casefold()

            allowed_special_owners = {
                self.user_name.casefold(),
                "unknown_participant",
            }

            if (
                owner_key not in allowed_special_owners
                and not _name_appears_in_source(
                    owner,
                    transcript,
                )
            ):
                # Named owner doesn't exist anywhere in the source.
                continue

            # ---------------------------------------------------------
            # Validate deadline source
            # ---------------------------------------------------------

            deadline = task.get("deadline")
            normalized_deadline = None
            if isinstance(deadline, dict):
                original = str(deadline.get("original") or task.get("original_deadline_phrase") or "").strip()
                normalized_deadline = deadline.get("normalized")
            else:
                original = str(deadline or task.get("original_deadline_phrase") or "").strip()
            if original:
                source_deadline = _find_source_quote(original, transcript)
                task["deadline"] = ({"original": source_deadline, "normalized": normalized_deadline}
                                    if source_deadline else None)
            else:
                task["deadline"] = None

            # ---------------------------------------------------------
            # Clamp confidence rather than trusting malformed values
            # ---------------------------------------------------------

            task["confidence"] = _categorical_level(task.get("confidence"), "low")
            task["priority"] = _categorical_level(task.get("priority"), "medium")

            # ---------------------------------------------------------
            # Deterministic deduplication
            # ---------------------------------------------------------

            deadline_key = ""

            if isinstance(task.get("deadline"), dict):
                deadline_key = _canonical_text(
                    task["deadline"].get("original", "")
                )

            dedupe_key = (
                _canonical_text(owner),
                _canonical_text(task_text),
                deadline_key,
            )

            if dedupe_key in seen_tasks:
                continue

            seen_tasks.add(dedupe_key)
            valid_tasks.append(task)

        raw["tasks"] = valid_tasks

        # -------------------------------------------------------------
        # Validate people mentioned
        # -------------------------------------------------------------

        valid_people: list[dict] = []
        seen_people: set[str] = set()

        people_values = raw.get(
            "people_mentioned",
            [],
        )

        if not isinstance(people_values, list):
            people_values = []

        blocked_names = {
            "other participant",
            "unknown_participant",
        }

        for person in people_values:
            if not isinstance(person, dict):
                continue

            name = str(
                person.get("name") or ""
            ).strip()

            context = str(
                person.get("context") or ""
            ).strip()

            if not name or not context:
                continue

            name_key = name.casefold()

            if name_key in blocked_names:
                continue

            # A real person's name must actually appear in the transcript.
            if not _name_appears_in_source(
                name,
                transcript,
            ):
                continue

            evidence = str(
                person.get("evidence") or ""
            ).strip()

            if evidence:
                verified = _find_source_quote(
                    evidence,
                    transcript,
                )

                if verified:
                    person["evidence"] = verified
                else:
                    # Evidence is optional for people, so remove invalid
                    # evidence rather than inventing another quote.
                    person.pop("evidence", None)

            person["importance"] = _categorical_level(person.get("importance"), "medium")

            canonical_name = _canonical_text(name)

            if canonical_name in seen_people:
                continue

            seen_people.add(canonical_name)
            valid_people.append(person)

        raw["people_mentioned"] = valid_people

        return raw
