import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path


class TranscriptionService:
    ECHO_TIME_PADDING_SECONDS = 4.0
    MIN_ECHO_WORDS = 5

    def __init__(self, model_name="large-v3", language=None, task="translate", initial_prompt=None, user_name="Hussain"):
        self.model_name = model_name
        self.language = language
        self.task = task
        self.initial_prompt = initial_prompt
        self.user_name = user_name
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

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

    def transcribe(self, audio_path: Path) -> dict:
        folder = Path(audio_path).parent
        sources = [(folder / "recording-system.wav", "Other participant"),
                   (folder / "recording-microphone.wav", self.user_name)]
        sources = [(path, label) for path, label in sources if path.exists()]
        if not sources:
            sources = [(Path(audio_path), "Speaker")]
        values, languages = [], []
        system_segments, microphone_segments = [], []
        for source, label in sources:
            segments, info = self._load().transcribe(
                str(source), vad_filter=True,
                vad_parameters={"threshold": 0.6, "min_silence_duration_ms": 500, "speech_pad_ms": 180},
                language=self.language, task=self.task, initial_prompt=self.initial_prompt,
                condition_on_previous_text=False, beam_size=5, best_of=5, temperature=[0.0, 0.2, 0.4],
                no_speech_threshold=0.6, log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4, hallucination_silence_threshold=2.0)
            languages.append(info.language)
            source_segments = [{"start": round(s.start, 3), "end": round(s.end, 3), "speaker": label,
                                "text": s.text.strip()} for s in segments if s.text.strip()]
            if source.name == "recording-system.wav":
                system_segments = source_segments
            elif source.name == "recording-microphone.wav":
                microphone_segments = source_segments
            else:
                values.extend(source_segments)

        removed_echoes = 0
        if system_segments and microphone_segments:
            microphone_segments, removed_echoes = self._remove_microphone_echoes(
                system_segments, microphone_segments
            )
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
        quality_score = max(0, min(100, round(100 - repeat_ratio * 80)))
        result = {"language": languages[0] if languages else "unknown",
                  "output_language": "en" if self.task == "translate" else (languages[0] if languages else "unknown"),
                  "model": self.model_name, "task": self.task,
                  "quality": {"score": quality_score, "word_count": word_count,
                              "removed_repetitions": total_removed,
                              "removed_cross_track_echoes": removed_echoes},
                  "segments": values}
        (folder / "transcript.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        (folder / "transcript.txt").write_text("\n".join(f"{x['speaker']}: {x['text']}" for x in values), encoding="utf-8")
        return result

    def availability(self):
        try:
            import faster_whisper  # noqa
            return {"available": True, "model": self.model_name, "task": self.task,
                    "language": self.language or "Automatic"}
        except ImportError:
            return {"available": False, "model": self.model_name, "message": "faster-whisper is not installed."}
