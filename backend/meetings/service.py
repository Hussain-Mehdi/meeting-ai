import asyncio
import json
import logging
import shutil
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
        self.llm = LLMService(
            settings.ollama_host, settings.ollama_model, settings.user_name, settings.aliases,
            settings.ollama_num_ctx,
        )
        self.started_at = None
        self._queue = None
        self._worker = None
        self._pending = []

    def start(self, title="Untitled meeting", meet_url=None):
        status = runtime.snapshot()
        if status["recording"]: raise ValueError("A recording is already in progress.")
        meeting_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        folder = self.settings.recordings_path / meeting_id; audio = folder / "recording.wav"
        self.recorder.start(audio)
        self.started_at = datetime.now().astimezone()
        self.db.create_meeting(meeting_id, title, self.started_at.isoformat(), audio, meet_url)
        runtime.transition(MeetingState.RECORDING, meeting_id=meeting_id, recording=True, meeting_detected=bool(meet_url),
                           stage_detail=f"Capturing system audio and microphone · {self.recorder.capture_backend}", error=None)
        notify("Recording started", title); log.info("recording started meeting=%s", meeting_id)
        return self.db.get_meeting(meeting_id)

    def stop(self):
        status = runtime.snapshot(); meeting_id = status["meeting_id"]
        if not status["recording"] or not meeting_id: raise ValueError("No recording is in progress.")
        result = self.recorder.stop(); ended = datetime.now().astimezone()
        duration = int((ended - self.started_at).total_seconds()) if self.started_at else int(result["duration_seconds"])
        self.db.finish_recording(meeting_id, ended.isoformat(), duration)
        runtime.transition(MeetingState.IDLE, meeting_id=None, recording=False, stage_detail="Recording saved")
        notify("Meeting ended", "Recording saved. Processing has started.")
        return {**result, "meeting_id": meeting_id}

    async def stop_and_process(self):
        """Finalize capture off-loop, then queue local processing so a new recording can start immediately."""
        result = await asyncio.to_thread(self.stop)
        self.enqueue(result["meeting_id"], prefer_saved_transcript=False)
        return result

    # ----- background processing queue -----

    def enqueue(self, meeting_id: str, prefer_saved_transcript: bool = False) -> str:
        """Queue a meeting for processing. Returns 'started' or 'queued'."""
        if self._queue is None:
            self._queue = asyncio.Queue()
        self._queue.put_nowait((meeting_id, prefer_saved_transcript))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._process_queue())
        if runtime.processing_busy():
            self._pending.append(meeting_id)
            runtime.processing_update(queued=list(self._pending))
            log.info("processing queued meeting=%s position=%s", meeting_id, len(self._pending))
            return "queued"
        runtime.processing_transition(MeetingState.RECORDED, meeting_id=meeting_id, progress=5,
                                      stage_detail="Recording saved", error=None, queued=[])
        return "started"

    async def _wait_for_recording_to_finish(self, meeting_id: str) -> None:
        """Whisper and Ollama saturate the CPU. Never start them while audio is being captured,
        so the recording, and therefore the transcript built from it, is never compromised."""
        waited = False
        while self.settings.defer_processing_while_recording and runtime.snapshot()["recording"]:
            if not waited:
                waited = True
                log.info("processing deferred while recording is active meeting=%s", meeting_id)
                runtime.processing_update(stage_detail="Waiting for the current recording to finish so audio capture keeps full priority")
            await asyncio.sleep(2)

    async def _process_queue(self):
        while True:
            meeting_id, prefer_saved_transcript = await self._queue.get()
            if meeting_id in self._pending:
                self._pending.remove(meeting_id)
            runtime.processing_update(queued=list(self._pending))
            try:
                await self._wait_for_recording_to_finish(meeting_id)
                await self.process(meeting_id, prefer_saved_transcript=prefer_saved_transcript)
            except Exception:
                log.exception("processing worker failed meeting=%s", meeting_id)
            finally:
                self._queue.task_done()

    async def watchdog(self, interval: float = 2.0):
        """Save a recording whose capture process died or that exceeded the maximum length."""
        while True:
            await asyncio.sleep(interval)
            status = runtime.snapshot()
            if not status["recording"]: continue
            meeting_id = status["meeting_id"]
            reason = self.recorder.health()
            if not reason and self.started_at:
                elapsed = (datetime.now().astimezone() - self.started_at).total_seconds()
                if elapsed > self.settings.max_recording_hours * 3600:
                    reason = f"Recording reached the {self.settings.max_recording_hours:g} hour limit and was saved automatically."
            if not reason: continue
            log.warning("watchdog stopping recording meeting=%s reason=%s", meeting_id, reason)
            notify("Recording stopped", reason, True)
            try:
                await self.stop_and_process()
            except Exception as exc:
                # The capture died before anything usable reached disk. Leave the row
                # retryable and make the reason visible instead of showing "Recording" forever.
                log.exception("watchdog could not save recording meeting=%s", meeting_id)
                message = f"{reason} {exc}"
                try: self.db.set_status(meeting_id, "failed", message)
                except Exception: log.exception("could not persist failed status meeting=%s", meeting_id)
                runtime.transition(MeetingState.FAILED, meeting_id=None, recording=False, stage_detail="Recording stopped", error=message)

    async def resume_interrupted(self, meeting_ids):
        """Continue processing meetings that a restart cut off, from their last checkpoint."""
        for meeting_id in meeting_ids:
            try:
                source = await self.retry(meeting_id)
                log.info("resuming interrupted meeting=%s from saved %s", meeting_id, source)
            except ValueError as exc:
                log.warning("could not resume interrupted meeting=%s: %s", meeting_id, exc)

    def delete_meeting(self, meeting_id: str) -> dict:
        """Permanently remove a meeting's database rows and its folder of recordings, transcript, and analysis."""
        meeting = self.db.get_meeting(meeting_id)
        if not meeting: raise KeyError("Meeting not found.")
        status = runtime.snapshot()
        if status["recording"] and status["meeting_id"] == meeting_id:
            raise ValueError("This meeting is being recorded. Stop the recording before deleting it.")
        if status["processing"]["meeting_id"] == meeting_id and status["processing"]["state"] in ("recorded", "transcribing", "analyzing"):
            raise ValueError("This meeting is being processed. Wait for processing to finish before deleting it.")
        if meeting_id in self._pending:
            raise ValueError("This meeting is queued for processing. Wait for it to finish before deleting it.")
        folder = Path(meeting["audio_path"]).parent if meeting.get("audio_path") else None
        removed_files = False
        recordings_root = self.settings.recordings_path.resolve()
        if folder and folder.exists() and folder.resolve().parent == recordings_root:
            shutil.rmtree(folder, ignore_errors=False)
            removed_files = True
        self.db.delete_meeting(meeting_id)
        if status["processing"]["meeting_id"] == meeting_id:
            runtime.processing_transition(MeetingState.IDLE, meeting_id=None, progress=0, stage_detail="Nothing to process", error=None)
        log.info("meeting deleted meeting=%s files_removed=%s", meeting_id, removed_files)
        return {"deleted": meeting_id, "files_removed": removed_files}

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
            runtime.processing_transition(MeetingState.TRANSCRIBING, meeting_id=meeting_id, progress=12, error=None,
                                          stage_detail="Loading the local Whisper model",
                                          processing_started_at=datetime.now(timezone.utc).isoformat())
            self.db.set_status(meeting_id, "transcribing")
            result = await asyncio.to_thread(self._load_saved_transcript, meeting) if prefer_saved_transcript else None
            if result:
                runtime.processing_update(progress=55, stage_detail=f"Saved transcript restored · {len(result['segments'])} segments")
            else:
                available_recordings = [path for path in self._recording_candidates(audio) if path.exists() and path.stat().st_size > 0]
                if not available_recordings:
                    raise RuntimeError("Neither a saved transcript nor a saved recording could be found.")
                levels = await asyncio.to_thread(self.recorder.measure_audio, available_recordings[0])
                if levels["has_audible_audio"] is False:
                    raise RuntimeError("The recording contains silence, so no transcript or summary was generated. Confirm the audio capture setup and make sure someone speaks during the test.")
                runtime.processing_update(progress=25, stage_detail="Transcribing speech locally")
                result = await asyncio.to_thread(self.transcriber.transcribe, audio)
                transcript_path = folder / "transcript.json"
                self.db.save_transcript(meeting_id, result["segments"], transcript_path)
            text = self._transcript_text(result["segments"])
            if not text:
                raise RuntimeError("Whisper found no speech in the recording. The recording is safe, but AI analysis was skipped to prevent an invented summary.")
            quality = result.get("quality", {})
            if quality.get("score", 100) < 45:
                raise RuntimeError("Transcript quality was too low for reliable AI analysis. The transcript and recording were saved, but summarization was skipped to avoid misleading results.")
            runtime.processing_update(progress=55, stage_detail=f"Transcript saved · {len(result['segments'])} segments · quality {quality.get('score', 100)}%")
            runtime.processing_transition(MeetingState.ANALYZING, progress=65, stage_detail="Extracting evidence from the transcript")
            self.db.set_status(meeting_id, "analyzing")
            started = datetime.fromisoformat(meeting["started_at"])
            ended = datetime.fromisoformat(meeting["ended_at"] or meeting["started_at"])
            metadata = {"title": meeting["title"], "date": started.date().isoformat(), "start_time": started.isoformat(),
                "end_time": ended.isoformat(), "duration_minutes": max(1, round(meeting["duration_seconds"] / 60))}
            def analysis_progress(value, detail): runtime.processing_update(progress=value, stage_detail=detail)
            analysis = await asyncio.to_thread(self.llm.analyze, text, metadata, analysis_progress)
            runtime.processing_update(progress=94, stage_detail="Validating and saving the meeting record")
            analysis_path = self._save_analysis_artifacts(folder, analysis)
            self.db.save_analysis(meeting_id, analysis, analysis_path)
            runtime.processing_transition(MeetingState.COMPLETED, progress=100, stage_detail="Meeting ready")
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
            runtime.processing_transition(MeetingState.FAILED, stage_detail="Processing stopped safely", error=message)
            notify("Processing failed", "Your saved recording or transcript can be retried in Meeting AI.", True)

    async def retry(self, meeting_id, retranscribe=False):
        meeting = self.db.get_meeting(meeting_id)
        if not meeting: raise ValueError("Meeting not found.")
        current = runtime.snapshot()
        if meeting_id == current["processing"]["meeting_id"] and current["processing"]["state"] in ("recorded", "transcribing", "analyzing"):
            raise ValueError("This meeting is already being processed.")
        if meeting_id in self._pending:
            raise ValueError("This meeting is already queued for processing.")
        saved_transcript = await asyncio.to_thread(self._load_saved_transcript, meeting)
        audio = Path(meeting["audio_path"])
        recording_available = any(path.exists() and path.stat().st_size > 0 for path in self._recording_candidates(audio))
        if retranscribe and not recording_available:
            raise ValueError("The saved recording could not be found, but the saved transcript may still be analyzed.")
        if not retranscribe and not saved_transcript and not recording_available:
            raise ValueError("Neither the saved transcript nor the saved recording could be found.")
        source = "recording" if retranscribe or not saved_transcript else "transcript"
        self.db.set_status(meeting_id, "transcribing")
        outcome = self.enqueue(meeting_id, prefer_saved_transcript=source == "transcript")
        if outcome == "started":
            runtime.processing_update(stage_detail=f"Retrying from saved {source}")
        return source
