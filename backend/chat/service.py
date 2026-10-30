"""Ask questions about your meetings.

Retrieval first, then a local model. Passages come from the meetings' own transcripts and
analyses, and the prompt makes the boundary explicit: anything presented as having happened in
a meeting must come from the passages and carry its citation, while general knowledge is
allowed but must be labelled as such. Nothing leaves the machine.
"""
import json
import logging
import re
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 9_000
MAX_HISTORY_MESSAGES = 8

SYSTEM_PROMPT = """You are {user_name}'s meeting assistant. You answer questions about meetings
that were recorded and transcribed locally, and you may also explain general concepts.

Today is {today}.

--------------------------------------------------

Anything about what was said, decided, asked, promised, or assigned in a meeting may come
only from the numbered passages below, and every such statement must carry the citation of
the passage it came from, written exactly like [1] or [2]. If the passages do not answer the
question, say plainly that the meetings do not cover it. Never guess, never fill gaps, never
merge two meetings into one event.

You may also answer from your own general knowledge — definitions, how something usually
works, options worth considering — but say so in your own words ("Generally, ...",
"Outside of your meetings, ..."). Never dress general knowledge up as something that was
discussed, and never attach a citation to it.

When a question has both parts (for example "what should I do about X"), give what the
meetings say first, with citations, then the general advice as a separate short paragraph.

--------------------------------------------------

- Never invent a task, owner, deadline, decision, attendee, or number.
- A person mentioned in a meeting is not necessarily an attendee. Do not promote them.
- Do not state who attended unless a passage says so.
- Quote sparingly and exactly; do not paraphrase a quote inside quotation marks.
- If passages disagree, say so and cite both.
- Prefer the most recent meeting when the question is about the current state of something,
  and say which meeting you used.
- Keep answers short and direct. Use short paragraphs or bullets. No preamble, no sign-off.
- Write the answer in your own plain words, as if speaking to {user_name}. Never print a
  section banner, a heading in capitals, or any label copied from these instructions.

==================================================
RETRIEVED PASSAGES
==================================================

{context}
"""

NO_CONTEXT_NOTE = """(No meeting passage matched this question. Say that the meetings do not
appear to cover it, then answer from general knowledge if that is useful, clearly labelled.)"""


def _when(started_at: str) -> str:
    try:
        return datetime.fromisoformat(started_at).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return str(started_at or "unknown date")


def _clock(seconds) -> str:
    total = int(max(0.0, float(seconds or 0)))
    return f"{total // 60}:{total % 60:02d}"


def build_context(hits: list[dict]) -> tuple[str, list[dict]]:
    """Render retrieved passages as numbered blocks, and the matching source list for the UI."""
    blocks, sources, used = [], [], 0
    for number, hit in enumerate(hits, start=1):
        content = str(hit.get("content") or "").strip()
        if not content:
            continue
        when = _when(hit.get("started_at"))
        where = f" at {_clock(hit['start'])}" if hit.get("kind") == "transcript" and hit.get("start") else ""
        header = f"[{number}] {hit.get('title') or 'Meeting'} — {when}{where} ({hit.get('heading') or hit.get('kind')})"
        block = f"{header}\n{content}"
        if used + len(block) > MAX_CONTEXT_CHARS:
            break
        blocks.append(block)
        used += len(block)
        sources.append({
            "n": number,
            "meeting_id": hit.get("meeting_id"),
            "meeting_title": hit.get("title"),
            "date": when,
            "kind": hit.get("kind"),
            "heading": hit.get("heading"),
            "start": hit.get("start") or 0,
            "excerpt": content[:300],
        })
    return "\n\n".join(blocks), sources


def used_citations(answer: str, sources: list[dict]) -> list[dict]:
    """Keep only the sources the answer actually cited, so the UI never shows unused ones."""
    cited = {int(n) for n in re.findall(r"\[(\d{1,2})\]", answer or "")}
    return [s for s in sources if s["n"] in cited] if cited else []


class ChatService:
    def __init__(self, db, llm, settings):
        self.db = db
        self.llm = llm
        self.settings = settings
        self.client = httpx.Client(timeout=httpx.Timeout(connect=5, read=600, write=30, pool=5))

    def retrieve(self, question: str, meeting_id: str | None = None, limit: int = 12) -> list[dict]:
        return self.db.search_index(question, limit=limit, meeting_id=meeting_id)

    def _messages(self, question: str, history: list[dict], context: str) -> list[dict]:
        system = SYSTEM_PROMPT.format(
            user_name=self.settings.user_name,
            today=datetime.now().strftime("%d %B %Y"),
            context=context or NO_CONTEXT_NOTE,
        )
        messages = [{"role": "system", "content": system}]
        for item in history[-MAX_HISTORY_MESSAGES:]:
            role = item.get("role")
            if role in ("user", "assistant") and str(item.get("content") or "").strip():
                messages.append({"role": role, "content": item["content"]})
        messages.append({"role": "user", "content": question})
        return messages

    def stream_answer(self, question: str, history: list[dict], context: str):
        """Yield answer chunks from Ollama as they arrive."""
        payload = {
            "model": self.llm.model,
            "messages": self._messages(question, history, context),
            "stream": True,
            "think": False,
            "options": {"temperature": 0.2, "num_ctx": self.llm.num_ctx},
            "keep_alive": "10m",
        }
        with self.client.stream("POST", f"{self.llm.host}/api/chat", json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                chunk = (data.get("message") or {}).get("content") or ""
                if chunk:
                    yield chunk
                if data.get("done"):
                    return

    def title_for(self, question: str) -> str:
        words = re.sub(r"\s+", " ", (question or "").strip())
        return (words[:60] + "…") if len(words) > 60 else (words or "New chat")
