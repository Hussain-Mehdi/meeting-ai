import asyncio
import json
import time
from datetime import date
from pathlib import Path
from fastapi.testclient import TestClient
from backend.analysis.dates import normalize_deadline
from backend.analysis.ollama import chunk_transcript
from backend.analysis.schemas import MeetingAnalysis
from backend.database.db import Database
from backend.audio.recorder import AudioRecorder, RecordingError
from backend.meetings.service import MeetingService
from backend.state import MeetingState, StateMachine


def test_deadlines():
    day = date(2026, 8, 10)
    assert normalize_deadline("tomorrow", day) == "2026-08-11"
    assert normalize_deadline("by Friday", day) == "2026-08-14"


def test_state_machine():
    state = StateMachine(); state.transition(MeetingState.RECORDING); state.transition(MeetingState.RECORDED)
    state.transition(MeetingState.TRANSCRIBING); state.transition(MeetingState.ANALYZING); state.transition(MeetingState.COMPLETED)
    assert state.snapshot()["state"] == "completed"


def test_database_persists(tmp_path):
    db = Database(tmp_path / "test.db"); db.create_meeting("a", "Test", "2026-08-10T10:00:00+05:00", tmp_path / "a.wav")
    assert db.get_meeting("a")["title"] == "Test"
    assert db.get_meeting("a")["can_retry"] is False


def test_interrupted_processing_becomes_retryable_failure(tmp_path):
    db = Database(tmp_path / "test.db")
    audio = tmp_path / "recording.wav"; audio.write_bytes(b"saved audio")
    db.create_meeting("a", "Interrupted", "2026-08-10T10:00:00+05:00", audio)
    db.set_status("a", "analyzing")
    assert db.recover_interrupted_meetings() == 1
    meeting = db.get_meeting("a")
    assert meeting["status"] == "failed"
    assert meeting["can_retry"] is True
    assert "latest safe checkpoint" in meeting["error"]


def test_saved_transcript_can_be_restored_without_whisper(tmp_path):
    folder = tmp_path / "meeting"; folder.mkdir()
    audio = folder / "recording.wav"
    db = Database(tmp_path / "test.db")
    db.create_meeting("a", "Test", "2026-08-10T10:00:00+05:00", audio)
    transcript_path = folder / "transcript.json"
    transcript_path.write_text(json.dumps({"quality": {"score": 91}, "segments": [
        {"start": 0.0, "end": 2.0, "speaker": "Hussain", "text": "I will prepare the release notes."}
    ]}), encoding="utf-8")
    service = MeetingService.__new__(MeetingService); service.db = db
    restored = service._load_saved_transcript(db.get_meeting("a"))
    assert restored["quality"]["score"] == 91
    assert restored["segments"][0]["speaker"] == "Hussain"
    assert db.get_meeting("a")["transcript_available"] is True


def test_retry_prefers_transcript_even_when_recording_is_missing(monkeypatch, tmp_path):
    import backend.meetings.service as meeting_service_module

    folder = tmp_path / "meeting"; folder.mkdir()
    db = Database(tmp_path / "test.db")
    db.create_meeting("a", "Test", "2026-08-10T10:00:00+05:00", folder / "missing-recording.wav")
    segments = [{"start": 0.0, "end": 1.0, "speaker": "Hussain", "text": "Use the saved transcript."}]
    transcript_path = folder / "transcript.json"
    transcript_path.write_text(json.dumps({"quality": {"score": 90}, "segments": segments}), encoding="utf-8")
    db.save_transcript("a", segments, transcript_path)
    service = MeetingService.__new__(MeetingService); service.db = db
    monkeypatch.setattr(meeting_service_module, "runtime", StateMachine())
    scheduled = []
    def capture_task(coroutine):
        scheduled.append(coroutine); coroutine.close()
    monkeypatch.setattr(meeting_service_module.asyncio, "create_task", capture_task)
    source = asyncio.run(service.retry("a"))
    assert source == "transcript"
    assert scheduled
    assert db.get_meeting("a")["status"] == "transcribing"


def test_preview_failure_keeps_original_recording(monkeypatch, tmp_path):
    class FinishedProcess:
        def poll(self): return 0

    source = tmp_path / "recording-system.wav"; source.write_bytes(b"saved audio")
    recorder = AudioRecorder(); recorder.process = FinishedProcess(); recorder.started_monotonic = time.monotonic() - 1
    recorder.path = tmp_path / "recording.wav"; recorder.source_paths = [source]
    def fail_preview(_duration): raise RecordingError("preview failed")
    monkeypatch.setattr(recorder, "_create_preview", fail_preview)
    monkeypatch.setattr(recorder, "measure_audio", lambda _path: {"has_audible_audio": True})
    result = recorder.stop()
    assert result["source_paths"] == [str(source)]
    assert "preview failed" in result["preview_error"]


def test_schema_and_chunking():
    payload={"meeting":{"title":"Test","date":"2026-08-10","start_time":"2026-08-10T10:00:00+05:00","end_time":"2026-08-10T10:10:00+05:00","duration_minutes":10},"summary":"A concise summary.","tasks":[],"goals":[],"key_topics":[],"decisions":[],"attendees":[],"people_mentioned":[],"next_steps":[]}
    assert MeetingAnalysis.model_validate(payload).meeting.title == "Test"
    assert "open_questions" not in MeetingAnalysis.model_json_schema()["properties"]
    assert len(chunk_transcript("x"*30000, 10000, 100)) > 1


def test_all_analysis_prompts_resolve_required_placeholders():
    from backend.analysis.prompts import CHUNK_PROMPT, FINAL_PROMPT
    from backend.analysis.ollama import LLMService

    service = LLMService("http://127.0.0.1:11434", "test", "Hussain")
    assert "{user_name}" not in service._system_prompt("2026-08-10")
    chunk = CHUNK_PROMPT.format(transcript="Hussain: I will prepare the report.", chunk_id="chunk_001", user_name="Hussain")
    assert "{user_name}" not in chunk
    final = FINAL_PROMPT.format(metadata="{}", schema="{}", candidates="[]", source_transcript="null", meeting_date="2026-08-10")
    assert "{meeting_date}" not in final


def test_llm_safety_sanitize():
    from backend.analysis.ollama import LLMService
    metadata={"title":"Test","date":"2026-08-10","start_time":"2026-08-10T10:00:00+05:00","end_time":"2026-08-10T10:10:00+05:00","duration_minutes":10}
    raw={"summary":"Discussed the release.","attendees":[{"name":"Invented","confidence":0.8}]}
    service=LLMService("http://127.0.0.1:11434","test","Hussain")
    repaired=service._sanitize(raw,"The team discussed the release.",metadata)
    result=MeetingAnalysis.model_validate(repaired)
    assert result.meeting.title == "Test"
    assert result.summary == "Discussed the release."
    assert result.attendees == []


def test_llm_repairs_numeric_task_confidence_and_candidate_shapes():
    from backend.analysis.ollama import LLMService
    metadata={"title":"Test","date":"2026-08-10","start_time":"2026-08-10T10:00:00+05:00","end_time":"2026-08-10T10:10:00+05:00","duration_minutes":10}
    transcript="Hussain: I will prepare the release report by Friday. Ahmed will review it."
    raw={"summary":["The release report was discussed."],
         "decisions":[{"decision":"Use the release report."}],
         "tasks":[{"owner":"Hussain","action":"Prepare the release report","deadline":"by Friday",
                   "priority":"normal","confidence":0.0,"evidence":"I will prepare the release report by Friday."}],
         "people_mentioned":[{"name":"Ahmed","context":"Will review the report.","importance":0.9}]}
    service=LLMService("http://127.0.0.1:11434","test","Hussain")
    result=MeetingAnalysis.model_validate(service._sanitize(raw,transcript,metadata))
    assert result.summary == "The release report was discussed."
    assert result.decisions == ["Use the release report."]
    assert result.tasks[0].confidence == "low"
    assert result.tasks[0].priority == "medium"
    assert result.tasks[0].deadline.original == "by Friday"
    assert result.people_mentioned[0].importance == "high"


def test_health(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api.db"))
    from backend.main import app
    assert TestClient(app).get("/api/health").json()["status"] == "ok"
