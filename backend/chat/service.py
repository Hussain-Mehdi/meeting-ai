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

from backend.chat.fusion import RRF_CANDIDATES, RRF_K, reciprocal_rank_fusion
from backend.chat.scope import detect_scope

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

If a WEB RESULTS section appears below, it is external information the user explicitly asked
for; cite it as [W1] and never as a meeting fact.

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
THE MEETINGS THEMSELVES (authoritative)
==================================================

This list comes straight from the database and is the only correct source for which
meetings exist, what they are called and when they happened. Never contradict it and never
invent a meeting or a date that is not on it. "The last meeting" means the first row here.

{roster}

==================================================
RETRIEVED PASSAGES (from the meetings)
==================================================

{context}
{web}"""

NO_CONTEXT_NOTE = """(No meeting passage matched this question. Say that the meetings do not
appear to cover it, then answer from general knowledge if that is useful, clearly labelled.)"""

ROSTER_LIMIT = 12

WEB_BLOCK = """
==================================================
WEB RESULTS (external - NOT from the meetings)
==================================================

These came from a web search run just now, because the user asked for it. They are outside
sources, not things anyone said in a meeting.

- Cite them as [W1], [W2] - a different marker from meeting passages on purpose.
- Never describe anything here as something that was said, decided, agreed or assigned in a
  meeting, and never let a web claim and a meeting fact share a sentence without making the
  difference explicit.
- They may be wrong or out of date; attribute them ("according to <site>") rather than
  asserting them.
- The user asked for a web answer, so judge it against the web. If these results do not answer
  the question, say that the search did not turn it up and suggest where to look — do NOT say
  "the meetings do not cover it", which answers a question nobody asked.

{results}
"""


def build_roster(meetings: list[dict]) -> str:
    """A compact, factual list of recent meetings: dates a question may refer to."""
    if not meetings:
        return "(No meetings have been recorded yet.)"
    lines = []
    for index, meeting in enumerate(meetings):
        minutes = round((meeting.get("duration_seconds") or 0) / 60)
        label = " (most recent)" if index == 0 else ""
        lines.append(f"- {_when(meeting.get('started_at'))}{label}: \"{meeting.get('title') or 'Meeting'}\", {minutes} min")
    return "\n".join(lines)


def _when(started_at: str) -> str:
    try:
        return datetime.fromisoformat(started_at).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return str(started_at or "unknown date")


def _clock(seconds) -> str:
    total = int(max(0.0, float(seconds or 0)))
    return f"{total // 60}:{total % 60:02d}"


def build_web_block(results) -> str:
    """Render web results in their own citation namespace, or nothing at all."""
    if not results:
        return ""
    blocks = [f"[W{result.n}] {result.title} - {result.domain}\n{result.snippet}" for result in results]
    return WEB_BLOCK.format(results="\n\n".join(blocks))


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
    """Keep only the meeting sources the answer cited, so the UI never shows unused ones.

    `[W1]`-style web markers are deliberately not matched here: the two namespaces stay
    separate all the way to the UI."""
    cited = {int(n) for n in re.findall(r"\[(\d{1,2})\]", answer or "")}
    return [s for s in sources if s["n"] in cited] if cited else []


def used_web_citations(answer: str, results) -> list[dict]:
    cited = {int(n) for n in re.findall(r"\[W(\d{1,2})\]", answer or "", flags=re.IGNORECASE)}
    return [r.as_dict() for r in results if r.n in cited] if cited else []


class ChatService:
    def __init__(self, db, llm, settings, vectors=None, web=None):
        self.db = db
        self.llm = llm
        self.settings = settings
        self.vectors = vectors
        self.web = web
        self.client = httpx.Client(timeout=httpx.Timeout(connect=5, read=600, write=30, pool=5))

    def _search(self, question: str, limit: int, meeting_id: str | None = None,
                meeting_ids: list[str] | None = None) -> list[dict]:
        """Keyword and semantic search over the same passages, fused by rank.

        With no vector index the fusion is a no-op by construction (see fusion.py), so this
        returns exactly what keyword search returned before semantic search existed.
        """
        lexical = self.db.search_index(question, limit=RRF_CANDIDATES,
                                       meeting_id=meeting_id, meeting_ids=meeting_ids)
        vectors = getattr(self, "vectors", None)
        if vectors is None:
            return lexical[:limit]
        dense = vectors.search(question, limit=RRF_CANDIDATES,
                               meeting_id=meeting_id, meeting_ids=meeting_ids)
        if not dense:
            return lexical[:limit]
        settings = self.settings
        return reciprocal_rank_fusion(
            [lexical, dense],
            weights=[float(getattr(settings, "bm25_weight", 1.0)), float(getattr(settings, "vector_weight", 1.0))],
            k=int(getattr(settings, "rrf_k", RRF_K)), limit=limit)

    def retrieve(self, question: str, meeting_id: str | None = None, limit: int = 12) -> list[dict]:
        """Find the passages a question needs.

        A question that names a meeting by time ("the last meeting", "yesterday", "29 Oct")
        is resolved to actual meetings by date first, and only then searched inside. Keyword
        search alone cannot do this: "last" and "meeting" are words people say constantly.
        """
        if meeting_id:
            hits = self._search(question, limit, meeting_id=meeting_id)
            return hits or self.db.meeting_passages(meeting_id, limit=limit)

        scope = detect_scope(question)
        scoped_ids: list[str] = []
        if scope["kind"] == "latest":
            scoped_ids = self.db.latest_meeting_ids(scope["count"])
        elif scope["kind"] == "range":
            scoped_ids = self.db.meeting_ids_between(scope["start"].isoformat(), scope["end"].isoformat())

        if not scoped_ids:
            return self._search(question, limit)

        # Inside the chosen meetings, prefer passages that match the question, but always
        # include each meeting's own material so "summarise it" works without keyword luck.
        # Both retrievers are given the scoped meetings: an unscoped semantic search would
        # pull similar-sounding passages from every meeting and reopen the bug scope.py fixed.
        matched = self._search(question, limit, meeting_ids=scoped_ids)
        per_meeting = max(3, limit // max(1, len(scoped_ids)))
        hits: list[dict] = []
        seen: set[tuple] = set()
        for scoped_id in scoped_ids:
            for hit in self.db.meeting_passages(scoped_id, limit=per_meeting):
                key = (hit["meeting_id"], hit["kind"], hit["ref"])
                if key not in seen:
                    seen.add(key)
                    hits.append(hit)
        for hit in matched:
            key = (hit["meeting_id"], hit["kind"], hit["ref"])
            if key not in seen:
                seen.add(key)
                hits.append(hit)
        log.info("chat scope=%s meetings=%s passages=%s", scope["kind"], len(scoped_ids), len(hits))
        return hits[:limit]

    def _messages(self, question: str, history: list[dict], context: str, roster: str = "",
                  web: str = "") -> list[dict]:
        system = SYSTEM_PROMPT.format(
            user_name=self.settings.user_name,
            today=datetime.now().strftime("%d %B %Y"),
            roster=roster or build_roster(getattr(self, "db", None) and self.db.recent_meetings(ROSTER_LIMIT) or []),
            context=context or NO_CONTEXT_NOTE,
            web=web,
        )
        messages = [{"role": "system", "content": system}]
        for item in history[-MAX_HISTORY_MESSAGES:]:
            role = item.get("role")
            if role in ("user", "assistant") and str(item.get("content") or "").strip():
                messages.append({"role": role, "content": item["content"]})
        messages.append({"role": "user", "content": question})
        return messages

    def stream_answer(self, question: str, history: list[dict], context: str, roster: str = "",
                      web: str = ""):
        """Yield answer chunks from Ollama as they arrive."""
        payload = {
            "model": self.llm.model,
            "messages": self._messages(question, history, context, roster, web),
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
