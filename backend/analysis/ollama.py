import json
import re
from datetime import date
from difflib import SequenceMatcher
from typing import Any

import httpx

from backend.analysis.dates import normalize_deadline
from backend.analysis.prompts import (
    SYSTEM_PROMPT,
    CHUNK_PROMPT,
    ACTION_AUDIT_PROMPT,
    FINAL_PROMPT,
)
from backend.analysis.schemas import MeetingAnalysis
from backend.analysis.templates import get_template


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
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "evidence": {"type": "string"},
                    "source_chunk_id": {"type": "string"},
                },
                "required": [
                    "owner",
                    "action",
                    "task",
                    "deadline",
                    "original_deadline_phrase",
                    "priority",
                    "status",
                    "confidence",
                    "evidence",
                    "source_chunk_id",
                ],
            },
        },
        "requested_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requested_of": {"type": "string"},
                    "change": {"type": "string"},
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
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "evidence": {"type": "string"},
                    "source_chunk_id": {"type": "string"},
                },
                "required": [
                    "requested_of",
                    "change",
                    "deadline",
                    "original_deadline_phrase",
                    "priority",
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
        "requested_changes",
        "next_steps",
        "important_entities",
    ],
}

ACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": CHUNK_JSON_SCHEMA["properties"]["tasks"],
        "requested_changes": CHUNK_JSON_SCHEMA["properties"]["requested_changes"],
    },
    "required": ["tasks", "requested_changes"],
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


def _canonical_evidence(value: str) -> str:
    """Canonicalize a quote while ignoring an optional transcript speaker prefix."""
    text = str(value or "").strip()
    prefix, separator, remainder = text.partition(":")
    if separator and len(prefix.split()) <= 4:
        text = remainder.strip()
    return _canonical_text(text)


def _merge_richer_action(existing: dict, candidate: dict) -> None:
    """Preserve stronger supported attributes when two records cite the same utterance."""
    if not existing.get("deadline") and candidate.get("deadline"):
        existing["deadline"] = candidate["deadline"]
    rank = {"low": 0, "medium": 1, "high": 2}
    for field in ("priority", "confidence"):
        if rank.get(candidate.get(field), 0) > rank.get(existing.get(field), 0):
            existing[field] = candidate[field]


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
        # Local models sometimes preserve the right quote with one or two words
        # normalized. Recover only strong token-level matches and always return
        # the original transcript wording, never the model's paraphrase.
        quote_words = _canonical_text(quote).split()
        if len(quote_words) < 5:
            return None
        best_match: tuple[float, str] | None = None
        for raw_line in source.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            utterance = line.split(":", 1)[1].strip() if ":" in line else line
            source_words = _canonical_text(utterance).split()
            if not source_words:
                continue
            matcher = SequenceMatcher(None, quote_words, source_words, autojunk=False)
            blocks = matcher.get_matching_blocks()
            matched = sum(block.size for block in blocks)
            longest = max((block.size for block in blocks), default=0)
            coverage = matched / len(quote_words)
            if coverage >= 0.78 and longest >= 4 and (not best_match or coverage > best_match[0]):
                best_match = (coverage, utterance)
        return best_match[1] if best_match else None

    return match.group(0)


def _source_context_for_quote(quote: str, source: str, radius: int = 1) -> str:
    """Return the source line and narrow adjacent context containing verified evidence."""
    quote_key = _canonical_text(quote)
    if not quote_key:
        return ""
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        line_key = _canonical_text(line)
        if quote_key in line_key or line_key.endswith(quote_key):
            start = max(0, index - radius)
            end = min(len(lines), index + radius + 1)
            return "\n".join(lines[start:end])
    return ""


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
        user_aliases: list[str] | None = None,
        num_ctx: int = 32_768,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.user_name = user_name
        self.user_aliases = {
            value.casefold() for value in [user_name, *(user_aliases or [])] if value.strip()
        }
        self.num_ctx = max(8_192, int(num_ctx))

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
        contains {meeting_date}. Meeting-type guidance and the speaker-label
        legend for the current meeting are appended; they never relax the
        source-of-truth rules above them.
        """

        values = {
            "user_name": self.user_name,
            "meeting_date": meeting_date or "unknown",
        }

        prompt = SYSTEM_PROMPT.format(**values)
        context = getattr(self, "_meeting_context", None) or {}
        template = get_template(context.get("template"))
        prompt += (
            "\n\n==================================================\n"
            f"MEETING TYPE: {template.name.upper()}\n"
            "==================================================\n\n"
            f"{template.guidance}\n"
        )
        speakers = context.get("speakers") or {}
        named = [name for name in speakers.get("named", []) if name]
        unnamed = [name for name in speakers.get("unnamed", []) if name]
        prompt += (
            "\n==================================================\n"
            "SPEAKER LABELS IN THIS TRANSCRIPT\n"
            "==================================================\n\n"
            f"- \"{self.user_name}\" is the user's own microphone.\n"
        )
        if unnamed:
            prompt += (
                f"- Unnamed voices: {', '.join(unnamed)}. These are distinct people detected by voice, "
                "not names. Treat each exactly like \"Other participant\": never turn the label into a "
                "person, never list it as an attendee, and assign its first-person commitments to "
                "\"unknown_participant\" unless the person's real name is explicitly established in speech.\n"
            )
        if named:
            prompt += (
                f"- Confirmed people: {', '.join(named)}. The user has confirmed these speaker names from "
                "their voices, so they ARE real attendees. A first-person commitment spoken under one of "
                "these labels belongs to that person; a request addressed to one of them is a task for them.\n"
            )
        return prompt

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
                        "num_ctx": self.num_ctx,
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

        self._meeting_context = {
            "template": metadata.get("template"),
            "speakers": metadata.get("speakers") or {},
        }
        # Only schema fields are shown to the model as authoritative metadata.
        metadata = {key: value for key, value in metadata.items() if key not in ("template", "speakers")}

        # -------------------------------------------------------------
        # Stage 1: evidence extraction
        # -------------------------------------------------------------

        candidates: list[dict[str, Any]] = []
        focused_actions: dict[str, list[dict]] = {"tasks": [], "requested_changes": []}

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

            if progress_callback:
                progress_callback(
                    70 + round(12 * index / max(1, len(chunks))),
                    f"Auditing actions in transcript part {index + 1} of {len(chunks)}",
                )

            action_prompt = ACTION_AUDIT_PROMPT.format(
                transcript=chunk,
                chunk_id=chunk_id,
                user_name=self.user_name,
            )
            action_evidence = self._generate_json(
                action_prompt,
                schema=ACTION_JSON_SCHEMA,
                meeting_date=metadata["date"],
            )

            for key in ("tasks", "requested_changes"):
                values = action_evidence.get(key, [])
                if isinstance(values, list):
                    focused_actions[key].extend(
                        item for item in values
                        if isinstance(item, dict)
                        and _categorical_level(item.get("confidence"), "low") in {"high", "medium"}
                    )

            for key in ("tasks", "requested_changes"):
                general_values = extracted.get(key, [])
                focused_values = action_evidence.get(key, [])
                if not isinstance(general_values, list):
                    general_values = []
                if not isinstance(focused_values, list):
                    focused_values = []
                extracted[key] = [*general_values, *focused_values]

            # Never trust a model-generated chunk ID.
            for task in extracted.get("tasks", []):
                if isinstance(task, dict):
                    task["source_chunk_id"] = chunk_id
            for change in extracted.get("requested_changes", []):
                if isinstance(change, dict):
                    change["source_chunk_id"] = chunk_id

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
            user_name=self.user_name,
        )

        last_error: Exception | None = None

        for attempt in range(2):
            try:
                generated = self._generate_json(
                    prompt,
                    schema=schema,
                    meeting_date=metadata["date"],
                )

                # The final editorial pass may shorten a report too aggressively.
                # Reintroduce medium/high-confidence candidates from the dedicated
                # action audit, then let the deterministic evidence and owner checks
                # reject unsupported records and remove duplicates.
                for key in ("tasks", "requested_changes"):
                    final_values = generated.get(key, [])
                    if not isinstance(final_values, list):
                        final_values = []
                    generated[key] = [*final_values, *focused_actions[key]]

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

        for change in analysis.requested_changes:
            deadline = getattr(change, "deadline", None)
            if deadline and deadline.original and not deadline.normalized:
                deadline.normalized = normalize_deadline(deadline.original, meeting_day)

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

            action = str(task.get("action") or "").strip()
            if action and task_text and not _canonical_text(task_text).startswith(_canonical_text(action)):
                task_text = f"{action} {task_text}"

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

            if owner_key in self.user_aliases:
                owner = self.user_name
                owner_key = owner.casefold()
                task["owner"] = owner

            owner_context = _source_context_for_quote(source_evidence, transcript)
            if owner_key == self.user_name.casefold():
                if not any(_name_appears_in_source(alias, owner_context) for alias in self.user_aliases):
                    continue
            elif owner_key not in {"unknown_participant", "team"} and not _name_appears_in_source(
                owner, owner_context
            ):
                # A named owner must be supported by the task evidence or its
                # immediately adjacent conversational context.
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

            duplicate_by_evidence = next((existing for existing in valid_tasks
                if _canonical_text(existing.get("owner", "")) == _canonical_text(owner)
                and _canonical_evidence(existing.get("evidence", "")) == _canonical_evidence(source_evidence)), None)
            if duplicate_by_evidence:
                _merge_richer_action(duplicate_by_evidence, task)
                continue

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
        # Validate requested changes independently from accepted tasks
        # -------------------------------------------------------------

        valid_changes: list[dict] = []
        seen_changes: set[tuple] = set()
        change_values = raw.get("requested_changes", [])
        if not isinstance(change_values, list):
            change_values = []

        for change in change_values:
            if not isinstance(change, dict):
                continue

            requested_of = str(change.get("requested_of") or "unknown_participant").strip()
            change_text = str(change.get("change") or change.get("task") or change.get("action") or "").strip()
            evidence = str(change.get("evidence") or "").strip()
            if not requested_of or not change_text or not evidence:
                continue

            source_evidence = _find_source_quote(evidence, transcript)
            if not source_evidence:
                continue

            recipient_key = requested_of.casefold()
            if recipient_key in self.user_aliases:
                requested_of = self.user_name
                recipient_key = requested_of.casefold()

            recipient_context = _source_context_for_quote(source_evidence, transcript)
            if recipient_key == self.user_name.casefold():
                if not any(_name_appears_in_source(alias, recipient_context) for alias in self.user_aliases):
                    continue
            elif recipient_key not in {"unknown_participant", "team", "all_participants"} and not _name_appears_in_source(
                requested_of, recipient_context
            ):
                continue

            deadline = change.get("deadline")
            normalized_deadline = None
            if isinstance(deadline, dict):
                original = str(deadline.get("original") or change.get("original_deadline_phrase") or "").strip()
                normalized_deadline = deadline.get("normalized")
            else:
                original = str(deadline or change.get("original_deadline_phrase") or "").strip()
            if original:
                source_deadline = _find_source_quote(original, transcript)
                change["deadline"] = ({"original": source_deadline, "normalized": normalized_deadline}
                                      if source_deadline else None)
            else:
                change["deadline"] = None

            change["requested_of"] = requested_of
            change["change"] = change_text
            change["evidence"] = source_evidence
            change["confidence"] = _categorical_level(change.get("confidence"), "low")
            change["priority"] = _categorical_level(change.get("priority"), "medium")

            duplicate_by_evidence = next((existing for existing in valid_changes
                if _canonical_text(existing.get("requested_of", "")) == _canonical_text(requested_of)
                and _canonical_evidence(existing.get("evidence", "")) == _canonical_evidence(source_evidence)), None)
            if duplicate_by_evidence:
                _merge_richer_action(duplicate_by_evidence, change)
                continue

            deadline_key = ""
            if isinstance(change.get("deadline"), dict):
                deadline_key = _canonical_text(change["deadline"].get("original", ""))
            dedupe_key = (
                _canonical_text(requested_of), _canonical_text(change_text), deadline_key
            )
            if dedupe_key in seen_changes:
                continue
            seen_changes.add(dedupe_key)
            valid_changes.append(change)

        raw["requested_changes"] = valid_changes

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
