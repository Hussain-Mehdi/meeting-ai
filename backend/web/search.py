"""Optional web search, so the assistant can answer about things newer than the model.

This is the only part of Meeting AI that sends anything off the machine, so the boundary is
deliberately narrow and checkable:

* it is off unless the user turns it on for a message;
* the query is the user's own message, passed through **verbatim** — there is no
  query-rewriting step, which is what makes "no meeting content leaves the machine" a
  property of the code rather than a promise;
* the query text is never written to the log;
* every failure returns no results, so a search problem can never fail a chat answer.
"""
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebResult:
    n: int
    title: str
    url: str
    snippet: str
    domain: str

    def as_dict(self) -> dict:
        return {"n": self.n, "title": self.title, "url": self.url,
                "snippet": self.snippet, "domain": self.domain}


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


class WebSearch:
    """A thin wrapper over ddgs. Deliberately thin: the less it does, the less can leak."""

    def __init__(self, settings):
        self.settings = settings
        self.enabled = bool(getattr(settings, "web_search_enabled", True))
        self.results = int(getattr(settings, "web_search_results", 5))
        self.timeout = float(getattr(settings, "web_search_timeout", 8.0))
        self.last_error: str | None = None

    def available(self) -> dict:
        if not self.enabled:
            return {"available": False, "backend": "ddgs",
                    "message": "Web search is switched off in settings."}
        try:
            import ddgs  # noqa: F401
        except ImportError:
            return {"available": False, "backend": "ddgs",
                    "message": "Web search needs the ddgs package: pip install ddgs"}
        return {"available": True, "backend": "ddgs", "message": self.last_error}

    def search(self, query: str, limit: int | None = None) -> list[WebResult]:
        """Search the web for exactly `query`. Returns [] on any failure, never raises."""
        query = str(query or "").strip()
        if not self.enabled or not query:
            return []
        limit = limit or self.results
        try:
            from ddgs import DDGS
            rows = DDGS(timeout=self.timeout).text(query, max_results=limit)
        except Exception as exc:
            # Rate limit, network failure, or an engine changing its markup. Never fatal.
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("web search failed (%s); answering from meetings only", type(exc).__name__)
            return []

        self.last_error = None
        results: list[WebResult] = []
        for row in rows or []:
            url = str(row.get("href") or "").strip()
            title = str(row.get("title") or "").strip()
            if not url or not title:
                continue
            results.append(WebResult(n=len(results) + 1, title=title[:200], url=url,
                                     snippet=str(row.get("body") or "").strip()[:500],
                                     domain=_domain(url)))
            if len(results) >= limit:
                break
        # The query itself is user content and is never logged.
        log.info("web search returned %s results", len(results))
        return results
