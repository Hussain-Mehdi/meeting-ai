"""Keeps the meeting corpus embedded, and answers nearest-neighbour queries over it.

At 4,478 passages a NumPy dot product takes well under a millisecond, so there is no vector
index here and no vector database — just a matrix rebuilt from SQLite whenever the data
underneath it changes.

Embedding happens on one background thread. It yields while a recording is in progress, for
the same reason transcription does: capture is the source of truth and gets the machine to
itself.
"""
import logging
import threading
import time

import numpy as np

from backend.chat.embeddings import EmbeddingClient, content_hash, embed_text, pack, unpack
from backend.state import runtime

log = logging.getLogger(__name__)


class VectorIndex:
    def __init__(self, db, client: EmbeddingClient, settings):
        self.db = db
        self.client = client
        self.settings = settings
        self.model = client.model
        self._pending: set[str] = set()
        self._full_sweep = False
        self._queued_at = 0.0
        self._wake = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._matrix_cache: tuple[np.ndarray, list[tuple[str, str, str]], np.ndarray, list[str]] | None = None
        self._built_at = -1
        self._lock = threading.Lock()
        self._warned_unavailable = False
        self.last_message: str | None = None

    # ----- lifecycle -----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="chat-embeddings", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._wake:
            self._wake.notify_all()

    def schedule(self, meeting_id: str | None = None) -> None:
        """Queue one meeting, or a full sweep when given None."""
        with self._wake:
            if meeting_id is None:
                self._full_sweep = True
            else:
                self._pending.add(meeting_id)
            self._queued_at = time.monotonic()
            self._wake.notify_all()

    # ----- the worker -----

    def _run(self) -> None:
        while not self._stopping.is_set():
            with self._wake:
                while not self._stopping.is_set() and not self._pending and not self._full_sweep:
                    self._wake.wait(timeout=30)
                if self._stopping.is_set():
                    return
                # A transcript review produces a burst of edits; wait for it to settle so one
                # meeting is not re-embedded once per deleted line.
                quiet = time.monotonic() - self._queued_at
                debounce = float(getattr(self.settings, "embedding_debounce_seconds", 20.0))
                if quiet < debounce:
                    self._wake.wait(timeout=debounce - quiet)
                    continue
                meeting_ids = sorted(self._pending)
                full = self._full_sweep
                self._pending.clear()
                self._full_sweep = False

            try:
                self._work(meeting_ids, full)
            except Exception:
                log.exception("embedding worker failed")
                self._stopping.wait(timeout=30)

    def _work(self, meeting_ids: list[str], full_sweep: bool) -> None:
        if not self._ready():
            # Put the work back and try again later; a model pulled mid-session is picked up.
            with self._wake:
                self._pending.update(meeting_ids)
                self._full_sweep = self._full_sweep or full_sweep
                self._queued_at = 0.0
            self._stopping.wait(timeout=float(getattr(self.settings, "embedding_retry_seconds", 300.0)))
            return

        targets = list(meeting_ids)
        if full_sweep:
            targets += [m for m in self.db.meetings_needing_vectors(self.model) if m not in targets]
        for meeting_id in targets:
            if self._stopping.is_set():
                return
            self._await_recording()
            try:
                self.sync_meeting(meeting_id)
            except Exception:
                log.exception("embedding failed meeting=%s", meeting_id)
            self._stopping.wait(timeout=0.2)

    def _await_recording(self) -> None:
        """Capture owns the machine: never embed while audio is being recorded."""
        if not getattr(self.settings, "defer_processing_while_recording", True):
            return
        waited = False
        while not self._stopping.is_set() and runtime.snapshot()["recording"]:
            if not waited:
                waited = True
                log.info("embedding paused while a recording is active")
            self._stopping.wait(timeout=2)

    def _ready(self) -> bool:
        status = self.client.availability()
        if not status["available"] or not status["model_installed"]:
            self.last_message = status["message"]
            if not self._warned_unavailable:
                self._warned_unavailable = True
                log.warning("semantic search is waiting: %s", status["message"])
            return False
        if self._warned_unavailable:
            log.info("embedding model is available again")
        self._warned_unavailable = False
        self.last_message = None
        return True

    # ----- embedding one meeting -----

    def sync_meeting(self, meeting_id: str) -> int:
        """Embed whatever this meeting is missing. Returns how many vectors were computed."""
        rows = self.db.index_rows_with_hashes(meeting_id, self.model)
        if not rows:
            return 0
        reusable = self.db.vectors_for_meeting(meeting_id, self.model)
        items: list[tuple[str, str, str, bytes]] = []
        needed: list[tuple[str, str, str, str]] = []      # kind, ref, hash, text
        for row in rows:
            text = embed_text(row)
            digest = content_hash(text)
            if row.get("have_hash") == digest:
                continue                                  # already embedded, unchanged
            blob = reusable.get(digest)
            if blob is not None:
                # The passage was re-cut (its ref moved) but its text is identical, so the
                # vector we already paid for still describes it.
                items.append((row["kind"], row["ref"], digest, blob))
            else:
                needed.append((row["kind"], row["ref"], digest, text))

        computed = 0
        if needed:
            vectors = self.client.embed_documents([text for _, _, _, text in needed])
            if vectors is None:
                log.warning("embedding unavailable mid-meeting, leaving %s queued", meeting_id)
                self.schedule(meeting_id)
                return 0
            for (kind, ref, digest, _), vector in zip(needed, vectors):
                items.append((kind, ref, digest, pack(vector)))
            computed = len(vectors)

        if items:
            self.db.replace_vectors(meeting_id, self.model, int(self.client.dim or 0), items)
        return computed

    # ----- search -----

    def _matrix(self):
        """(matrix, keys, meeting index per row, meeting ids), rebuilt when the data changes."""
        with self._lock:
            if self._matrix_cache is not None and self._built_at == self.db.index_version:
                return self._matrix_cache
            dim = self.client.dim or int(getattr(self.settings, "embedding_dim", 0) or 0)
            if not dim:
                row = next((d for m, d, _ in self.db.vector_models() if m == self.model), 0)
                dim = row
            if not dim:
                return None
            rows = self.db.load_vectors(self.model, dim)
            if not rows:
                self._matrix_cache = None
                self._built_at = self.db.index_version
                return None
            keys, meeting_ids, meeting_of = [], [], []
            matrix = np.empty((len(rows), dim), dtype=np.float32)
            index_of: dict[str, int] = {}
            written = 0
            for meeting_id, kind, ref, blob in rows:
                vector = unpack(blob, dim)
                if vector is None:
                    continue
                matrix[written] = vector
                keys.append((meeting_id, kind, ref))
                if meeting_id not in index_of:
                    index_of[meeting_id] = len(meeting_ids)
                    meeting_ids.append(meeting_id)
                meeting_of.append(index_of[meeting_id])
                written += 1
            if not written:
                self._matrix_cache = None
                self._built_at = self.db.index_version
                return None
            self._matrix_cache = (matrix[:written], keys, np.asarray(meeting_of, dtype=np.int32), meeting_ids)
            self._built_at = self.db.index_version
            self.client.dim = dim
            return self._matrix_cache

    def search(self, question: str, limit: int = 30, meeting_id: str | None = None,
               meeting_ids: list[str] | None = None) -> list[dict]:
        """Nearest passages by cosine similarity. Returns [] on any failure — never raises."""
        try:
            built = self._matrix()
            if built is None:
                return []
            matrix, keys, meeting_of, known = built
            query = self.client.embed_query(question)
            if query is None or query.shape[0] != matrix.shape[1]:
                return []

            scores = matrix @ query
            wanted = [m for m in ([meeting_id] if meeting_id else (meeting_ids or [])) if m in known]
            if (meeting_id or meeting_ids) and not wanted:
                return []
            if wanted:
                allowed = np.asarray([known.index(m) for m in wanted], dtype=np.int32)
                scores = np.where(np.isin(meeting_of, allowed), scores, -np.inf)

            take = min(limit, scores.shape[0])
            top = np.argpartition(-scores, take - 1)[:take] if take < scores.shape[0] else np.arange(scores.shape[0])
            top = top[np.argsort(-scores[top])]
            floor = float(getattr(self.settings, "vector_min_score", 0.0) or 0.0)
            chosen = [keys[i] for i in top if np.isfinite(scores[i]) and scores[i] > floor]
            return self.db.rows_by_keys(chosen)
        except Exception:
            log.exception("semantic search failed, falling back to keyword search")
            return []

    # ----- reporting -----

    def check_model(self) -> None:
        """Drop vectors from a previous model so a model change rebuilds cleanly."""
        dim = self.client.dim or int(getattr(self.settings, "embedding_dim", 0) or 0)
        others = [(m, d, n) for m, d, n in self.db.vector_models() if m != self.model or (dim and d != dim)]
        if others:
            removed = self.db.drop_other_vectors(self.model, dim or -1)
            log.warning("embedding model changed to %s, dropped %s vectors from %s",
                        self.model, removed, ", ".join(f"{m}({d})" for m, d, _ in others))

    def status(self) -> dict:
        embedded, total = self.db.vector_counts(self.model)
        return {"enabled": True, "model": self.model, "dim": self.client.dim,
                "embedded": embedded, "total": total, "pending": max(0, total - embedded),
                "available": self.last_message is None,
                "message": self.last_message or (
                    "Indexing…" if embedded < total else "Ready"),
                "last_error": self.client.last_error}
