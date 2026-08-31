import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from backend.analysis.ollama import LLMService
from backend.audio.recorder import AudioRecorder
from backend.notifications.macos import notify
from backend.state import MeetingState, runtime
from backend.transcription.whisper import TranscriptionService


log = logging.getLogger(__name__)


class MeetingService:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db
        self.recorder = AudioRecorder()
        self.transcriber = TranscriptionService(
            settings.whisper_model,
            language=settings.whisper_language or None,
            task=settings.whisper_task,
            initial_prompt=settings.whisper_initial_prompt,
            user_name=settings.user_name,
        )
        self.llm = LLMService(settings.ollama_host, settings.ollama_model, settings.user_name)
        self.started_at = None

    def start(self, title="Untitled meeting", meet_url=None):
        status = runtime.snapshot()
        if status["recording"]: raise ValueError("A recording is already in progress.")
        if status["state"] in ("recorded", "transcribing", "analyzing"):
            raise ValueError("Wait for the current meeting to finish processing before starting another recording.")
        meeting_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        folder = self.settings.recordings_path / meeting_id; audio = folder / "recording.wav"
        self.recorder.start(audio)
        self.started_at = datetime.now().astimezone()
        self.db.create_meeting(meeting_id, title, self.started_at.isoformat(), audio, meet_url)
        runtime.transition(MeetingState.RECORDING, meeting_id=meeting_id, recording=True, meeting_detected=bool(meet_url), progress=0,
                           stage_detail=f"Capturing system audio and microphone · {self.recorder.capture_backend}", processing_started_at=None, error=None)
        notify("Recording started", title); log.info("recording started meeting=%s", meeting_id)
        return self.db.get_meeting(meeting_id)

    def stop(self):
        status = runtime.snapshot(); meeting_id = status["meeting_id"]
        if not meeting_id: raise ValueError("No recording is in progress.")
        result = self.recorder.stop(); ended = datetime.now().astimezone()
        duration = int((ended - self.started_at).total_seconds()) if self.started_at else int(result["duration_seconds"])
        self.db.finish_recording(meeting_id, ended.isoformat(), duration)
        runtime.transition(MeetingState.RECORDED, recording=False, progress=5, stage_detail="Recording saved")
        notify("Meeting ended", "Recording saved. Processing has started.")
        return {**result, "meeting_id": meeting_id}

    async def stop_and_process(self):
        """Finalize ffmpeg off-loop, then schedule local processing on the active app loop."""
        result = await asyncio.to_thread(self.stop)
        asyncio.create_task(self.process(result["meeting_id"], prefer_saved_transcript=False))
        return result

    @staticmethod
    def _recording_candidates(audio: Path) -> list[Path]:
        return [audio, audio.parent / "recording-system.wav", audio.parent / "recording-microphone.wav"]

    def _load_saved_transcript(self, meeting: dict) -> dict | None:
        """Restore a transcript from SQLite or disk without invoking Whisper again."""
        meeting_id = meeting["id"]
        audio = Path(meeting["audio_path"])
        transcript_path = Path(meeting.get("transcript_path") or audio.parent / "transcript.json")
        payload = {}
        if transcript_path.exists():
            try:
                value = json.loads(transcript_path.read_text(encoding="utf-8"))
                if isinstance(value, dict): payload = value
            except (OSError, json.JSONDecodeError):
                log.warning("saved transcript JSON could not be read meeting=%s", meeting_id)
        segments = self.db.transcript(meeting_id)
        if not segments:
            file_segments = payload.get("segments", [])
            if isinstance(file_segments, list) and file_segments:
                segments = [segment for segment in file_segments if isinstance(segment, dict) and str(segment.get("text", "")).strip()]
                if segments:
                    self.db.save_transcript(meeting_id, segments, transcript_path)
        if not segments:
            return None
        return {
            "segments": segments,
            "quality": payload.get("quality", {"score": 100}),
            "language": payload.get("language", "unknown"),
            "output_language": payload.get("output_language", "unknown"),
            "model": payload.get("model", "saved transcript"),
            "task": payload.get("task", "saved transcript"),
        }

    @staticmethod
    def _transcript_text(segments: list[dict]) -> str:
        return "\n".join(
            f"{segment.get('speaker') or 'Speaker'}: {str(segment.get('text', '')).strip()}"
            for segment in segments if str(segment.get("text", "")).strip()
        ).strip()

    @staticmethod
    def _write_json_safely(path: Path, content: str) -> None:
        """Write a validated artifact atomically so interruption cannot corrupt the previous copy."""
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    def _save_analysis_artifacts(self, folder: Path, analysis) -> Path:
        content = analysis.model_dump_json(indent=2)
        attempts = folder / "analysis-attempts"
        attempts.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        analysis_path = folder / "analysis.json"
        if analysis_path.exists():
            try:
                previous = analysis_path.read_text(encoding="utf-8")
                self._write_json_safely(attempts / f"analysis-{stamp}-previous.json", previous)
            except OSError:
                log.warning("previous analysis could not be archived folder=%s", folder)
        attempt_path = attempts / f"analysis-{stamp}.json"
        self._write_json_safely(attempt_path, content)
        self._write_json_safely(analysis_path, content)
        return analysis_path

    def _write_recovery_record(self, meeting_id: str, folder: Path | None, error: Exception) -> str:
        meeting = self.db.get_meeting(meeting_id)
        audio = Path(meeting["audio_path"]) if meeting and meeting.get("audio_path") else None
        recording_available = bool(audio and any(path.exists() and path.stat().st_size > 0 for path in self._recording_candidates(audio)))
        transcript_available = bool(meeting and self._load_saved_transcript(meeting))
        if recording_available and transcript_available:
            safety = "Your recording and transcript are safe and can be retried."
        elif transcript_available:
            safety = "Your transcript is safe and can be analyzed again."
        elif recording_available:
            safety = "Your recording is safe and can be transcribed again."
        else:
            safety = "Your meeting metadata remains saved, but no usable recording or transcript was found."
        message = f"Processing failed. {safety} {error}"
        if folder:
            record = {
                "meeting_id": meeting_id,
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(error),
                "recording_available": recording_available,
                "transcript_available": transcript_available,
                "retry_from": "transcript" if transcript_available else "recording" if recording_available else None,
            }
            try:
                self._write_json_safely(folder / "recovery.json", json.dumps(record, indent=2))
            except Exception:
                log.exception("could not write recovery record meeting=%s", meeting_id)
        return message

    async def process(self, meeting_id, prefer_saved_transcript=False):
        folder = None
        try:
            meeting = self.db.get_meeting(meeting_id)
            if not meeting: raise ValueError("Meeting not found.")
            audio = Path(meeting["audio_path"]); folder = audio.parent
            runtime.transition(MeetingState.TRANSCRIBING, progress=12, stage_detail="Loading the local Whisper model",
                               processing_started_at=datetime.now(timezone.utc).isoformat())
            self.db.set_status(meeting_id, "transcribing")
            result = await asyncio.to_thread(self._load_saved_transcript, meeting) if prefer_saved_transcript else None
            if result:
                runtime.update(progress=55, stage_detail=f"Saved transcript restored · {len(result['segments'])} segments")
            else:
                available_recordings = [path for path in self._recording_candidates(audio) if path.exists() and path.stat().st_size > 0]
                if not available_recordings:
                    raise RuntimeError("Neither a saved transcript nor a saved recording could be found.")
                levels = await asyncio.to_thread(self.recorder.measure_audio, available_recordings[0])
                if levels["has_audible_audio"] is False:
                    raise RuntimeError("The recording contains silence, so no transcript or summary was generated. Confirm the audio capture setup and make sure someone speaks during the test.")
                runtime.update(progress=25, stage_detail="Transcribing speech locally")
                result = await asyncio.to_thread(self.transcriber.transcribe, audio)
                transcript_path = folder / "transcript.json"
                self.db.save_transcript(meeting_id, result["segments"], transcript_path)
            text = self._transcript_text(result["segments"])
            if not text:
                raise RuntimeError("Whisper found no speech in the recording. The recording is safe, but AI analysis was skipped to prevent an invented summary.")
            quality = result.get("quality", {})
            if quality.get("score", 100) < 45:
                raise RuntimeError("Transcript quality was too low for reliable AI analysis. The transcript and recording were saved, but summarization was skipped to avoid misleading results.")
            runtime.update(progress=55, stage_detail=f"Transcript saved · {len(result['segments'])} segments · quality {quality.get('score', 100)}%")
            runtime.transition(MeetingState.ANALYZING, progress=65, stage_detail="Extracting evidence from the transcript")
            self.db.set_status(meeting_id, "analyzing")
            started = datetime.fromisoformat(meeting["started_at"])
            ended = datetime.fromisoformat(meeting["ended_at"] or meeting["started_at"])
            metadata = {"title": meeting["title"], "date": started.date().isoformat(), "start_time": started.isoformat(),
                "end_time": ended.isoformat(), "duration_minutes": max(1, round(meeting["duration_seconds"] / 60))}
            def analysis_progress(value, detail): runtime.update(progress=value, stage_detail=detail)
            analysis = await asyncio.to_thread(self.llm.analyze, text, metadata, analysis_progress)
            runtime.update(progress=94, stage_detail="Validating and saving the meeting record")
            analysis_path = self._save_analysis_artifacts(folder, analysis)
            self.db.save_analysis(meeting_id, analysis, analysis_path)
            runtime.transition(MeetingState.COMPLETED, progress=100, stage_detail="Meeting ready")
            notify("Meeting ready", f"{analysis.meeting.title} has been processed. {len(analysis.tasks)} tasks found.", True)
        except Exception as exc:
            log.exception("processing failed meeting=%s", meeting_id)
            try:
                message = self._write_recovery_record(meeting_id, folder, exc)
            except Exception:
                log.exception("recovery inspection failed meeting=%s", meeting_id)
                message = f"Processing failed, but any recording and transcript files already saved were not deleted. {exc}"
            try: self.db.set_status(meeting_id, "failed", message)
            except Exception: log.exception("could not persist failed status meeting=%s", meeting_id)
            runtime.transition(MeetingState.FAILED, recording=False, stage_detail="Processing stopped safely",
                               error=message)
            notify("Processing failed", "Your saved recording or transcript can be retried in Meeting AI.", True)

    async def retry(self, meeting_id, retranscribe=False):
        meeting = self.db.get_meeting(meeting_id)
        if not meeting: raise ValueError("Meeting not found.")
        current = runtime.snapshot()
        if current["state"] in ("recording", "recorded", "transcribing", "analyzing"):
            raise ValueError("Another recording or meeting process is already running.")
        if current["state"] in ("detected", "waiting_for_confirmation"):
            runtime.transition(MeetingState.IDLE, meeting_detected=False, detected_title=None, detected_url=None)
        saved_transcript = await asyncio.to_thread(self._load_saved_transcript, meeting)
        audio = Path(meeting["audio_path"])
        recording_available = any(path.exists() and path.stat().st_size > 0 for path in self._recording_candidates(audio))
        if retranscribe and not recording_available:
            raise ValueError("The saved recording could not be found, but the saved transcript may still be analyzed.")
        if not retranscribe and not saved_transcript and not recording_available:
            raise ValueError("Neither the saved transcript nor the saved recording could be found.")
        source = "recording" if retranscribe or not saved_transcript else "transcript"
        runtime.transition(MeetingState.TRANSCRIBING, meeting_id=meeting_id, recording=False, progress=10,
                           stage_detail=f"Retrying from saved {source}", processing_started_at=datetime.now(timezone.utc).isoformat(), error=None)
        self.db.set_status(meeting_id, "transcribing")
        asyncio.create_task(self.process(meeting_id, prefer_saved_transcript=source == "transcript"))
        return source
