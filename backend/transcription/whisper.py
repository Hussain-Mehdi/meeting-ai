import json
import logging
import os
import platform
import re
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path


log = logging.getLogger(__name__)

# Whisper decoding settings shared by both backends. Same VAD, same thresholds, same
# temperature fallback, so switching backend changes speed, not the decoding policy.
VAD_SETTINGS = {"threshold": 0.6, "min_silence_duration_ms": 500, "speech_pad_ms": 180}
TEMPERATURES = (0.0, 0.2, 0.4)
NO_SPEECH_THRESHOLD = 0.6
LOG_PROB_THRESHOLD = -1.0
COMPRESSION_RATIO_THRESHOLD = 2.4
LOW_CONFIDENCE_LOG_PROB = -0.8
SAMPLE_RATE = 16_000

MLX_REPOS = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


def mlx_available() -> bool:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except Exception:
        return False


class TranscriptionService:
    ECHO_TIME_PADDING_SECONDS = 4.0
    MIN_ECHO_WORDS = 5

    def __init__(self, model_name="large-v3", language=None, task="transcribe", initial_prompt=None, user_name="Hussain",
                 backend="auto", translation="auto", mlx_repo=None, offline=True, diarizer=None):
        self.model_name = model_name
        self.language = language
        # `task` is kept for backwards compatibility: "translate" means the old behaviour of
        # translating at transcription time (English only, source wording lost).
        self.task = task if task in ("transcribe", "translate") else "transcribe"
        self.translation = translation if translation in ("auto", "always", "never") else "auto"
        self.initial_prompt = initial_prompt
        self.user_name = user_name
        self.backend = self._resolve_backend(backend)
        self.mlx_repo = mlx_repo or MLX_REPOS.get(model_name, f"mlx-community/whisper-{model_name}-mlx")
        self.offline = offline
        self.diarizer = diarizer
        self._model = None
        self._offline_applied = False
        self._apply_offline_mode()

    @staticmethod
    def _resolve_backend(backend: str) -> str:
        if backend == "mlx" or (backend == "auto" and mlx_available()):
            return "mlx"
        return "faster"

    # ----- model loading -----

    @staticmethod
    def _hub_cache_dir() -> Path:
        if os.environ.get("HF_HUB_CACHE"):
            return Path(os.environ["HF_HUB_CACHE"])
        if os.environ.get("HF_HOME"):
            return Path(os.environ["HF_HOME"]) / "hub"
        return Path.home() / ".cache" / "huggingface" / "hub"

    def _weights_cached(self, repo: str) -> bool:
        snapshots = self._hub_cache_dir() / f"models--{repo.replace('/', '--')}" / "snapshots"
        return snapshots.is_dir() and any(any(child.iterdir()) for child in snapshots.iterdir() if child.is_dir())

    def _apply_offline_mode(self) -> None:
        """Once the weights are cached, never contact the network for them again.

        huggingface_hub reads HF_HUB_OFFLINE when it is imported, so this runs from
        __init__, before faster-whisper or mlx-whisper are imported, using a plain
        filesystem check."""
        if self._offline_applied or not self.offline:
            return
        self._offline_applied = True
        repo = self.mlx_repo if self.backend == "mlx" else f"Systran/faster-whisper-{self.model_name}"
        if self._weights_cached(repo):
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        else:
            log.info("whisper weights for %s are not cached yet; the first transcription will download them", repo)

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

    # ----- per-track decoding -----

    def _prepare_mlx_audio(self, path: Path):
        """Apply the same VAD as faster-whisper, then keep a map back to original time."""
        import numpy as np
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import SpeechTimestampsMap, VadOptions, get_speech_timestamps
        audio = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
        chunks = get_speech_timestamps(audio, VadOptions(**VAD_SETTINGS), sampling_rate=SAMPLE_RATE)
        if not chunks:
            return None, None, 0.0
        speech = np.concatenate([audio[c["start"]:c["end"]] for c in chunks])
        removed = (len(audio) - len(speech)) / SAMPLE_RATE
        return speech, SpeechTimestampsMap(chunks, SAMPLE_RATE), removed

    def _decode_mlx(self, prepared, task: str) -> tuple[list[dict], str]:
        import mlx_whisper
        speech, timestamps, _ = prepared
        result = mlx_whisper.transcribe(
            speech, path_or_hf_repo=self.mlx_repo, language=self.language, task=task,
            initial_prompt=self.initial_prompt, condition_on_previous_text=False,
            temperature=TEMPERATURES, no_speech_threshold=NO_SPEECH_THRESHOLD,
            logprob_threshold=LOG_PROB_THRESHOLD, compression_ratio_threshold=COMPRESSION_RATIO_THRESHOLD,
            verbose=None)
        segments = []
        for s in result.get("segments", []):
            text = str(s.get("text", "")).strip()
            if not text:
                continue
            start = timestamps.get_original_time(float(s["start"]))
            end = timestamps.get_original_time(float(s["end"]))
            segments.append({"start": round(start, 3), "end": round(max(end, start), 3), "text": text,
                             "avg_logprob": round(float(s.get("avg_logprob", 0.0)), 3),
                             "no_speech_prob": round(float(s.get("no_speech_prob", 0.0)), 3)})
        return segments, result.get("language") or "unknown"

    def _decode_faster(self, path: Path, task: str) -> tuple[list[dict], str]:
        segments, info = self._load().transcribe(
            str(path), vad_filter=True, vad_parameters=dict(VAD_SETTINGS),
            language=self.language, task=task, initial_prompt=self.initial_prompt,
            condition_on_previous_text=False, beam_size=5, best_of=5, temperature=list(TEMPERATURES),
            no_speech_threshold=NO_SPEECH_THRESHOLD, log_prob_threshold=LOG_PROB_THRESHOLD,
            compression_ratio_threshold=COMPRESSION_RATIO_THRESHOLD, hallucination_silence_threshold=2.0)
        values = []
        for s in segments:
            text = s.text.strip()
            if not text:
                continue
            values.append({"start": round(s.start, 3), "end": round(s.end, 3), "text": text,
                           "avg_logprob": round(float(getattr(s, "avg_logprob", 0.0) or 0.0), 3),
                           "no_speech_prob": round(float(getattr(s, "no_speech_prob", 0.0) or 0.0), 3)})
        return values, getattr(info, "language", None) or "unknown"

    def _transcribe_track(self, path: Path) -> tuple[list[dict], str, str]:
        """Return (segments, language, output_language) with original-language text and, when
        the meeting is not in English, an aligned English rendering in `text_en`."""
        primary_task = self.task
        if self.backend == "mlx":
            prepared = self._prepare_mlx_audio(path)
            if prepared[0] is None:
                return [], "unknown", "unknown"
            decode = lambda task: self._decode_mlx(prepared, task)
        else:
            decode = lambda task: self._decode_faster(path, task)
        segments, language = decode(primary_task)
        output_language = "en" if primary_task == "translate" else language
        if primary_task == "transcribe" and segments:
            needs_english = self.translation == "always" or (self.translation == "auto" and language not in ("en", "unknown"))
            if needs_english:
                english, _ = decode("translate")
                self._attach_translation(segments, english)
            else:
                for segment in segments:
                    segment["text_en"] = segment["text"]
        return segments, language, output_language

    @staticmethod
    def _attach_translation(segments: list[dict], english: list[dict]) -> None:
        """Align a separate English pass onto the original-language segments by time overlap.
        The original text is never modified; the English rendering is an addition."""
        for segment in segments:
            start, end = segment["start"], segment["end"]
            inside = [e for e in english if start - 0.3 <= (e["start"] + e["end"]) / 2 <= end + 0.3]
            if not inside:
                overlaps = [(min(end, e["end"]) - max(start, e["start"]), e) for e in english]
                best = max(overlaps, key=lambda item: item[0], default=(0, None))
                inside = [best[1]] if best[1] is not None and best[0] > 0 else []
            segment["text_en"] = " ".join(e["text"] for e in inside).strip() or segment["text"]

    # ----- cross-track cleanup (unchanged policy) -----

    @staticmethod
    def _words(text: str) -> list[str]:
        """Normalize text for cross-track comparison while keeping Unicode words."""
        return re.findall(r"[^\W_]+(?:['’-][^\W_]+)?", text.casefold(), flags=re.UNICODE)

    @classmethod
    def _is_microphone_echo(cls, microphone_text: str, system_text: str) -> bool:
        """Detect the same speech despite punctuation, small errors, and different chunk sizes."""
        microphone_words = cls._words(microphone_text)
        system_words = cls._words(system_text)
        if len(microphone_words) < cls.MIN_ECHO_WORDS or not system_words:
            return False

        matcher = SequenceMatcher(None, microphone_words, system_words, autojunk=False)
        blocks = matcher.get_matching_blocks()
        matched_words = sum(block.size for block in blocks)
        longest_match = max((block.size for block in blocks), default=0)
        microphone_coverage = matched_words / len(microphone_words)

        # A long contiguous phrase is strong evidence of speaker-to-microphone
        # leakage. Very high total coverage also handles one or two recognition
        # errors inside an otherwise duplicated sentence.
        return ((microphone_coverage >= 0.70 and longest_match >= 4)
                or microphone_coverage >= 0.86)

    @classmethod
    def _remove_microphone_echoes(cls, system_segments: list[dict], microphone_segments: list[dict]):
        """Prefer system audio when the microphone contains a delayed copy of it.

        Whisper often places different boundaries around the same speech on each
        track. Compare each microphone segment with all system text in its expanded
        time window instead of comparing only whole, adjacent segments.
        """
        kept, removed = [], 0
        padding = cls.ECHO_TIME_PADDING_SECONDS
        for microphone in microphone_segments:
            microphone_start = float(microphone.get("start", 0))
            microphone_end = float(microphone.get("end", microphone_start))
            nearby_system = [
                segment for segment in system_segments
                if float(segment.get("end", segment.get("start", 0))) >= microphone_start - padding
                and float(segment.get("start", 0)) <= microphone_end + padding
            ]
            system_window = " ".join(segment.get("text", "") for segment in nearby_system)
            if cls._is_microphone_echo(microphone.get("text", ""), system_window):
                removed += 1
            else:
                kept.append(microphone)
        return kept, removed

    # ----- full pipeline -----

    def transcribe(self, audio_path: Path) -> dict:
        folder = Path(audio_path).parent
        started = time.monotonic()
        sources = [(folder / "recording-system.wav", "Other participant"),
                   (folder / "recording-microphone.wav", self.user_name)]
        sources = [(path, label) for path, label in sources if path.exists()]
        if not sources:
            sources = [(Path(audio_path), "Speaker")]
        values, languages, output_languages = [], [], []
        system_segments, microphone_segments = [], []
        for source, label in sources:
            segments, language, output_language = self._transcribe_track(source)
            languages.append(language); output_languages.append(output_language)
            for segment in segments:
                segment["speaker"] = label
                segment["track"] = "system" if source.name == "recording-system.wav" else "microphone" if source.name == "recording-microphone.wav" else "mix"
            if source.name == "recording-system.wav":
                system_segments = segments
            elif source.name == "recording-microphone.wav":
                microphone_segments = segments
            else:
                values.extend(segments)

        removed_echoes = 0
        if system_segments and microphone_segments:
            microphone_segments, removed_echoes = self._remove_microphone_echoes(
                system_segments, microphone_segments
            )
        diarization = None
        if system_segments and self.diarizer is not None:
            try:
                diarization = self.diarizer.label(folder / "recording-system.wav", system_segments)
            except Exception:
                log.exception("speaker diarization failed; keeping the single 'Other participant' label")
        values.extend(system_segments)
        values.extend(microphone_segments)
        values.sort(key=lambda segment: (segment["start"], segment["end"]))
        # Whisper can loop a short phrase across long low-energy regions. Cap exact
        # repeats and collapse adjacent duplicates without altering unique speech.
        cleaned, counts = [], Counter()
        removed_repeats = 0
        for segment in values:
            key = re.sub(r"[^\w]+", "", segment["text"].casefold())
            if not key: continue
            if cleaned and key == re.sub(r"[^\w]+", "", cleaned[-1]["text"].casefold()) and segment["speaker"] == cleaned[-1]["speaker"]:
                removed_repeats += 1
                continue
            counts[(segment["speaker"], key)] += 1
            if counts[(segment["speaker"], key)] > 3:
                removed_repeats += 1
                continue
            cleaned.append(segment)
        values = cleaned
        word_count = sum(len(item["text"].split()) for item in values)
        total_removed = removed_repeats + removed_echoes
        total_before = len(values) + total_removed
        repeat_ratio = total_removed / total_before if total_before else 0
        low_confidence = sum(1 for item in values if item.get("avg_logprob", 0) < LOW_CONFIDENCE_LOG_PROB
                             or item.get("no_speech_prob", 0) > 0.5)
        low_confidence_ratio = low_confidence / len(values) if values else 0
        quality_score = max(0, min(100, round(100 - repeat_ratio * 80 - low_confidence_ratio * 20)))
        language = languages[0] if languages else "unknown"
        result = {"language": language,
                  "output_language": output_languages[0] if output_languages else "unknown",
                  "model": self.model_name, "task": self.task, "backend": self.backend,
                  "translation": self.translation,
                  "quality": {"score": quality_score, "word_count": word_count,
                              "removed_repetitions": total_removed,
                              "removed_cross_track_echoes": removed_echoes,
                              "low_confidence_segments": low_confidence},
                  "speakers": diarization or {},
                  "timing_seconds": round(time.monotonic() - started, 1),
                  "segments": values}
        (folder / "transcript.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        (folder / "transcript.txt").write_text("\n".join(f"{x['speaker']}: {x['text']}" for x in values), encoding="utf-8")
        log.info("transcription finished backend=%s language=%s segments=%s seconds=%s", self.backend, language,
                 len(values), result["timing_seconds"])
        return result

    def availability(self):
        info = {"model": self.model_name, "task": self.task, "backend": self.backend, "translation": self.translation,
                "language": self.language or "Automatic"}
        if self.backend == "mlx":
            return {**info, "available": True, "engine": f"mlx-whisper on Metal · {self.mlx_repo}"}
        try:
            import faster_whisper  # noqa
            return {**info, "available": True, "engine": "faster-whisper on CPU (int8)"}
        except ImportError:
            return {**info, "available": False, "message": "faster-whisper is not installed."}
