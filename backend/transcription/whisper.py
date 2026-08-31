import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path


class TranscriptionService:
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

    def transcribe(self, audio_path: Path) -> dict:
        folder = Path(audio_path).parent
        sources = [(folder / "recording-system.wav", "Other participant"),
                   (folder / "recording-microphone.wav", self.user_name)]
        sources = [(path, label) for path, label in sources if path.exists()]
        if not sources:
            sources = [(Path(audio_path), "Speaker")]
        values, languages = [], []
        for source, label in sources:
            segments, info = self._load().transcribe(
                str(source), vad_filter=True,
                vad_parameters={"threshold": 0.6, "min_silence_duration_ms": 500, "speech_pad_ms": 180},
                language=self.language, task=self.task, initial_prompt=self.initial_prompt,
                condition_on_previous_text=False, beam_size=5, best_of=5, temperature=[0.0, 0.2, 0.4],
                no_speech_threshold=0.6, log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4, hallucination_silence_threshold=2.0)
            languages.append(info.language)
            values.extend({"start": round(s.start, 3), "end": round(s.end, 3), "speaker": label,
                           "text": s.text.strip()} for s in segments if s.text.strip())
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
            # Laptop speakers can leak remote speech into the microphone. Remove a
            # near-identical microphone copy when a system-audio segment exists at
            # nearly the same timestamp; retain Hussain's unique microphone speech.
            duplicate = False
            for earlier in reversed(cleaned[-6:]):
                if abs(segment["start"] - earlier["start"]) > 3.0: continue
                if segment["speaker"] == earlier["speaker"]: continue
                similarity = SequenceMatcher(None, segment["text"].casefold(), earlier["text"].casefold()).ratio()
                if similarity >= 0.86 and segment["speaker"] == self.user_name:
                    duplicate = True
                    break
            if duplicate:
                removed_repeats += 1
                continue
            cleaned.append(segment)
        values = cleaned
        word_count = sum(len(item["text"].split()) for item in values)
        total_before = len(values) + removed_repeats
        repeat_ratio = removed_repeats / total_before if total_before else 0
        quality_score = max(0, min(100, round(100 - repeat_ratio * 80)))
        result = {"language": languages[0] if languages else "unknown",
                  "output_language": "en" if self.task == "translate" else (languages[0] if languages else "unknown"),
                  "model": self.model_name, "task": self.task,
                  "quality": {"score": quality_score, "word_count": word_count, "removed_repetitions": removed_repeats},
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
