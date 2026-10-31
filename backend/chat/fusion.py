"""Reciprocal Rank Fusion: combine two rankings without having to compare their scores.

BM25 scores and cosine similarities live on different scales and are not comparable, so they
are fused by *rank* instead. The property that makes this safe to roll out: with a single
non-empty ranking the scores are strictly decreasing in rank, so the output order is exactly
the input order. Semantic search being unavailable therefore does not approximate today's
behaviour — it reproduces it.
"""
RRF_K = 60            # the standard constant; large enough that agreement between lists dominates
RRF_CANDIDATES = 30   # how deep each retriever goes before fusing


def fusion_key(hit: dict) -> tuple[str, str, str]:
    """The identity an index row has everywhere: meeting, kind, ref."""
    return (str(hit.get("meeting_id")), str(hit.get("kind")), str(hit.get("ref")))


def reciprocal_rank_fusion(rankings: list[list[dict]], weights: list[float] | None = None,
                           k: int = RRF_K, limit: int = 12) -> list[dict]:
    """score(d) = sum over lists of weight / (k + rank), rank starting at 1."""
    if weights is None:
        weights = [1.0] * len(rankings)
    scores: dict[tuple, float] = {}
    best: dict[tuple, dict] = {}
    order: dict[tuple, int] = {}
    ordinal = 0

    for index, ranking in enumerate(rankings):
        weight = weights[index] if index < len(weights) else 1.0
        for rank, hit in enumerate(ranking, start=1):
            key = fusion_key(hit)
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
            if key not in best:
                best[key] = hit
                order[key] = ordinal
                ordinal += 1

    fused = sorted(best.values(), key=lambda hit: (-scores[fusion_key(hit)], order[fusion_key(hit)]))
    return fused[:limit]
