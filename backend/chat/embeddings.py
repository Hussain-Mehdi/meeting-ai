"""Turn passages and questions into vectors, using the Ollama that is already running.

No new model runtime and no vector database: Ollama exposes `/api/embed`, and at this corpus
size similarity is a NumPy dot product. Every failure here returns None rather than raising,
because semantic search is an enhancement — chat must keep working on keyword search alone
when the embedding model is missing, loading, or unreachable.
"""
import hashlib
import logging
import struct

import httpx
import numpy as np

log = logging.getLogger(__name__)

# Some models are trained with asymmetric task prefixes and lose real accuracy without them.
# nomic-embed-text is the common case; bge-m3 and most others take none.
PREFIXES: dict[str, tuple[str, str]] = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
}

MAX_EMBED_CHARS = 4_000      # longest real passage is ~4.3k chars; neither model truncates here
QUERY_TIMEOUT = 5.0          # a slow embed must never stall a chat answer
BATCH_TIMEOUT = 120.0
KEEP_ALIVE = "30m"           # without this Ollama unloads between turns and the next query pays a load


def prefixes(model: str) -> tuple[str, str]:
    """(query prefix, document prefix) for a model, matched on its base name."""
    return PREFIXES.get(model.split(":")[0], ("", ""))


def embed_text(row: dict) -> str:
    """The exact text that represents an indexed row to the embedding model."""
    heading = str(row.get("heading") or "").strip()
    content = str(row.get("content") or "").strip()
    return (f"{heading}\n{content}" if heading else content)[:MAX_EMBED_CHARS]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def unpack(blob: bytes, dim: int) -> np.ndarray | None:
    if len(blob) != dim * 4:
        return None
    return np.frombuffer(blob, dtype="<f4")


def normalize(values) -> np.ndarray | None:
    """L2-normalise so cosine similarity is a plain dot product. None for a zero vector."""
    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm == 0.0:
        return None
    return (vector / norm).astype(np.float32)


class EmbeddingClient:
    def __init__(self, host: str, model: str, batch_size: int = 16):
        self.host = host.rstrip("/")
        self.model = model
        self.batch_size = max(1, int(batch_size))
        self.dim: int | None = None
        self.last_error: str | None = None
        self.client = httpx.Client(timeout=httpx.Timeout(connect=5, read=BATCH_TIMEOUT, write=30, pool=5))

    def availability(self) -> dict:
        """Mirrors LLMService.availability so the settings page can show both the same way."""
        try:
            response = self.client.get(f"{self.host}/api/tags", timeout=5)
            response.raise_for_status()
            names = [m.get("name", "") for m in response.json().get("models", []) if isinstance(m, dict)]
            base = self.model.split(":")[0]
            installed = self.model in names or any(name.split(":")[0] == base for name in names)
            return {"available": True, "model_installed": installed, "model": self.model, "dim": self.dim,
                    "message": None if installed else
                    f"Ollama is running, but the embedding model '{self.model}' is not installed. "
                    f"Run: ollama pull {self.model}"}
        except httpx.ConnectError:
            return {"available": False, "model_installed": False, "model": self.model, "dim": self.dim,
                    "message": "Unable to connect to Ollama."}
        except Exception as exc:
            return {"available": False, "model_installed": False, "model": self.model, "dim": self.dim,
                    "message": f"Ollama did not answer: {exc}"}

    def _post(self, inputs: list[str], timeout: float) -> list[np.ndarray] | None:
        try:
            response = self.client.post(
                f"{self.host}/api/embed",
                json={"model": self.model, "input": inputs, "keep_alive": KEEP_ALIVE},
                timeout=timeout)
            if response.status_code != 200:
                self.last_error = f"Ollama answered {response.status_code} for /api/embed"
                return None
            raw = response.json().get("embeddings")
            if not isinstance(raw, list) or len(raw) != len(inputs):
                self.last_error = "Ollama returned an unexpected embedding payload"
                return None
            vectors = [normalize(values) for values in raw]
            if any(vector is None for vector in vectors):
                self.last_error = "Ollama returned an empty embedding"
                return None
            dims = {vector.shape[0] for vector in vectors}
            if len(dims) != 1:
                self.last_error = "Ollama returned embeddings of mixed size"
                return None
            self.dim = dims.pop()
            self.last_error = None
            return vectors
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def embed_documents(self, texts: list[str]) -> list[np.ndarray] | None:
        if not texts:
            return []
        _, document_prefix = prefixes(self.model)
        vectors: list[np.ndarray] = []
        for start in range(0, len(texts), self.batch_size):
            batch = [document_prefix + text for text in texts[start:start + self.batch_size]]
            result = self._post(batch, BATCH_TIMEOUT)
            if result is None:
                return None
            vectors.extend(result)
        return vectors

    def embed_query(self, text: str) -> np.ndarray | None:
        query_prefix, _ = prefixes(self.model)
        result = self._post([query_prefix + str(text or "")[:MAX_EMBED_CHARS]], QUERY_TIMEOUT)
        return result[0] if result else None
