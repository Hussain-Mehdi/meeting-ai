"""Speaker diarization for the system-audio track (remote participants).

Every Whisper segment gets a speaker embedding (CAM++ via sherpa-onnx, fully local),
the embeddings are clustered into voices, and each voice is matched against the
named voice profiles the user has confirmed in earlier meetings. Unmatched voices are
labelled "Speaker N"; they are never given an invented name.
"""
import logging
import math
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
MODEL_NAME = "wespeaker_en_voxceleb_CAM++_LM.onnx"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL_NAME}"
MIN_EMBED_SECONDS = 0.8      # shorter utterances inherit the nearest voice in time
MAX_EMBED_SECONDS = 20.0     # enough voice for a stable embedding; keeps extraction fast
MIN_VOICE_SECONDS = 8.0      # a "voice" with less speech than this is noise; it joins its nearest voice


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


def _mean(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    return [sum(v[i] for v in vectors) / n for i in range(len(vectors[0]))]


class OnnxEmbeddingExtractor:
    """Thin wrapper so the diarizer can be unit-tested with a fake extractor."""

    def __init__(self, model_dir: Path):
        self.model_path = Path(model_dir) / MODEL_NAME
        self._extractor = None

    def _ensure_model(self) -> None:
        if self.model_path.exists():
            return
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        log.info("downloading speaker embedding model to %s", self.model_path)
        temporary = self.model_path.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL, temporary)
        temporary.replace(self.model_path)

    def _load(self):
        if self._extractor is None:
            import sherpa_onnx
            self._ensure_model()
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self.model_path), num_threads=2, provider="cpu")
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        return self._extractor

    def embed(self, samples) -> list[float]:
        extractor = self._load()
        stream = extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        return list(extractor.compute(stream))


class SpeakerDiarizer:
    def __init__(self, db, threshold: float = 0.62, match_threshold: float = 0.70, extractor=None,
                 model_dir: Path = Path("data/models")):
        self.db = db
        self.threshold = threshold              # cosine *distance* at which two voices are considered different
        self.match_threshold = match_threshold  # cosine *similarity* required to reuse a confirmed name
        self.extractor = extractor or OnnxEmbeddingExtractor(model_dir)

    # ----- clustering -----

    @staticmethod
    def _cluster(embeddings: list[list[float]], distance_threshold: float) -> list[int]:
        """Average-linkage agglomerative clustering on cosine distance. Returns a cluster index per embedding."""
        if len(embeddings) == 1:
            return [0]
        import numpy as np
        from scipy.cluster.hierarchy import fcluster, linkage
        matrix = np.asarray(embeddings, dtype=np.float64)
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
        links = linkage(matrix, method="average", metric="cosine")
        labels = fcluster(links, t=distance_threshold, criterion="distance")
        return [int(label) - 1 for label in labels]

    def _embed_segments(self, audio, segments: list[dict]) -> dict[int, list[float]]:
        import numpy as np
        embeddings = {}
        for index, segment in enumerate(segments):
            start, end = float(segment["start"]), float(segment["end"])
            if end - start < MIN_EMBED_SECONDS:
                continue
            end = min(end, start + MAX_EMBED_SECONDS)
            samples = audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
            if len(samples) < int(MIN_EMBED_SECONDS * SAMPLE_RATE):
                continue
            embeddings[index] = self.extractor.embed(np.ascontiguousarray(samples, dtype=np.float32))
        return embeddings

    def label(self, audio_path: Path, segments: list[dict]) -> dict:
        """Assign `speaker_id`/`speaker` to each segment in place. Returns a summary per voice."""
        if not segments:
            return {}
        from faster_whisper.audio import decode_audio
        audio = decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE)
        embeddings = self._embed_segments(audio, segments)
        if not embeddings:
            return {}
        indices = sorted(embeddings)
        clusters = self._cluster([embeddings[i] for i in indices], self.threshold)
        cluster_of = dict(zip(indices, clusters))
        # Short segments inherit the voice of the closest embedded segment in time.
        for index, segment in enumerate(segments):
            if index in cluster_of:
                continue
            middle = (float(segment["start"]) + float(segment["end"])) / 2
            nearest = min(indices, key=lambda i: abs((float(segments[i]["start"]) + float(segments[i]["end"])) / 2 - middle))
            cluster_of[index] = cluster_of[nearest]
        seconds, centroids = self._merge_tiny_voices(segments, embeddings, indices, cluster_of)
        # Order voices by speaking time so "Speaker 1" is the most active remote participant.
        ordered = sorted(seconds, key=lambda c: -seconds[c])
        rank = {cluster: position + 1 for position, cluster in enumerate(ordered)}
        names = self._match_profiles(centroids, rank)
        summary = {}
        for cluster in ordered:
            speaker_id = f"spk_{rank[cluster]}"
            matched = names.get(cluster)
            label = matched["name"] if matched else f"Speaker {rank[cluster]}"
            summary[speaker_id] = {"label": label, "name": matched["name"] if matched else None,
                                   "similarity": matched["similarity"] if matched else None,
                                   "seconds": round(seconds[cluster], 1),
                                   "segments": sum(1 for c in cluster_of.values() if c == cluster),
                                   "centroid": [round(v, 5) for v in centroids[cluster]]}
        for index, segment in enumerate(segments):
            speaker_id = f"spk_{rank[cluster_of[index]]}"
            segment["speaker_id"] = speaker_id
            segment["speaker"] = summary[speaker_id]["label"]
        log.info("diarization found %s voices (%s named from profiles)", len(summary), sum(1 for v in summary.values() if v["name"]))
        return summary

    @staticmethod
    def _merge_tiny_voices(segments, embeddings, indices, cluster_of) -> tuple[dict, dict]:
        """Fold clusters with almost no speech into the nearest real voice. Mutates cluster_of."""
        def totals():
            seconds: dict[int, float] = {}
            for index, cluster in cluster_of.items():
                seconds[cluster] = seconds.get(cluster, 0.0) + max(0.0, float(segments[index]["end"]) - float(segments[index]["start"]))
            return seconds
        def centroid_map(clusters):
            return {cluster: _mean([embeddings[i] for i in indices if cluster_of[i] == cluster]) for cluster in clusters}
        seconds = totals()
        while len(seconds) > 1:
            smallest = min(seconds, key=lambda c: seconds[c])
            if seconds[smallest] >= MIN_VOICE_SECONDS:
                break
            centroids = centroid_map(seconds)
            target = max((c for c in seconds if c != smallest), key=lambda c: _cosine(centroids[smallest], centroids[c]))
            for index, cluster in list(cluster_of.items()):
                if cluster == smallest:
                    cluster_of[index] = target
            seconds = totals()
        return seconds, centroid_map(seconds)

    def _match_profiles(self, centroids: dict[int, list[float]], rank: dict[int, int]) -> dict[int, dict]:
        """Greedy best-first matching of voices to confirmed profiles; one name per voice and per profile."""
        profiles = self.db.voice_profiles() if self.db else []
        if not profiles:
            return {}
        candidates = []
        for cluster, centroid in centroids.items():
            for profile in profiles:
                similarity = _cosine(centroid, profile["embedding"])
                if similarity >= self.match_threshold:
                    candidates.append((similarity, cluster, profile["name"]))
        candidates.sort(reverse=True)
        assigned, used_names, result = set(), set(), {}
        for similarity, cluster, name in candidates:
            if cluster in assigned or name in used_names:
                continue
            assigned.add(cluster); used_names.add(name)
            result[cluster] = {"name": name, "similarity": round(similarity, 3)}
        return result

    # ----- one-time naming -----

    def remember(self, name: str, centroid: list[float]) -> dict:
        """Store a confirmed name for a voice so future meetings label it automatically."""
        return self.db.upsert_voice_profile(name.strip(), centroid)
