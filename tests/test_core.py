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
from backend.transcription.whisper import TranscriptionService


def test_deadlines():
    day = date(2026, 8, 10)
    assert normalize_deadline("tomorrow", day) == "2026-08-11"
    assert normalize_deadline("by Friday", day) == "2026-08-14"


def test_state_machine():
    state = StateMachine(); state.transition(MeetingState.RECORDING); state.transition(MeetingState.IDLE)
    state.processing_transition(MeetingState.RECORDED); state.processing_transition(MeetingState.TRANSCRIBING)
    state.processing_transition(MeetingState.ANALYZING); state.processing_transition(MeetingState.COMPLETED)
    assert state.snapshot()["state"] == "idle"
    assert state.snapshot()["processing"]["state"] == "completed"


def test_recording_can_start_while_processing_runs():
    import pytest
    state = StateMachine()
    state.processing_transition(MeetingState.RECORDED); state.processing_transition(MeetingState.TRANSCRIBING)
    state.transition(MeetingState.RECORDING, recording=True)
    assert state.processing_busy() and state.snapshot()["recording"]
    with pytest.raises(ValueError): state.processing_transition(MeetingState.COMPLETED)
    state.transition(MeetingState.IDLE, recording=False)
    state.processing_transition(MeetingState.ANALYZING); state.processing_transition(MeetingState.COMPLETED)


def test_second_meeting_queues_behind_active_processing(monkeypatch, tmp_path):
    import backend.meetings.service as meeting_service_module
    db = Database(tmp_path / "test.db")
    for meeting_id in ("a", "b"):
        audio = tmp_path / meeting_id / "recording.wav"; audio.parent.mkdir(); audio.write_bytes(b"audio")
        db.create_meeting(meeting_id, meeting_id, "2026-08-10T10:00:00+05:00", audio)
    service = MeetingService.__new__(MeetingService); service.db = db
    service._queue = service._worker = None; service._pending = []
    service.settings = type("S", (), {"defer_processing_while_recording": True})()
    machine = StateMachine(); monkeypatch.setattr(meeting_service_module, "runtime", machine)
    processed = []

    async def fake_process(meeting_id, prefer_saved_transcript=False, analyze=True):
        processed.append(meeting_id)
        machine.processing_transition(MeetingState.TRANSCRIBING, meeting_id=meeting_id)
        await asyncio.sleep(0)
        machine.processing_transition(MeetingState.ANALYZING); machine.processing_transition(MeetingState.COMPLETED)
    service.process = fake_process

    async def scenario():
        assert service.enqueue("a") == "started"
        assert service.enqueue("b") == "queued"
        assert machine.snapshot()["processing"]["queued"] == ["b"]
        await service._queue.join()
        return machine.snapshot()["processing"]
    final = asyncio.run(scenario())
    assert processed == ["a", "b"]
    assert final["state"] == "completed" and final["meeting_id"] == "b" and final["queued"] == []


def test_database_persists(tmp_path):
    db = Database(tmp_path / "test.db"); db.create_meeting("a", "Test", "2026-08-10T10:00:00+05:00", tmp_path / "a.wav")
    assert db.get_meeting("a")["title"] == "Test"
    assert db.get_meeting("a")["can_retry"] is False


def _analysis(title="AI title", summary="Original summary", person="Ahmed"):
    return MeetingAnalysis.model_validate({
        "meeting": {"title": title, "date": "2026-08-10", "start_time": "2026-08-10T10:00:00+05:00",
                    "end_time": "2026-08-10T10:10:00+05:00", "duration_minutes": 10},
        "summary": summary, "goals": ["Ship safely"], "key_topics": ["Release"],
        "decisions": ["Use staged rollout"], "attendees": [],
        "people_mentioned": [{"name": person, "context": "Will review the release.", "importance": "high"}],
        "tasks": [{"owner": "Hussain", "task": "Prepare release notes", "priority": "high",
                   "confidence": "high", "evidence": "I will prepare the release notes."}],
        "requested_changes": [{"requested_of": "Hussain", "change": "Update the release screen",
                               "priority": "medium", "confidence": "high",
                               "evidence": "Hussain, please update the release screen."}],
        "next_steps": ["Review the release"],
    })


def test_completed_meeting_info_edit_does_not_change_report_or_time(tmp_path):
    db = Database(tmp_path / "test.db")
    db.create_meeting("a", "Initial title", "2026-08-10T10:00:00+05:00", tmp_path / "a.wav")
    db.finish_recording("a", "2026-08-10T10:10:00+05:00", 600)
    db.save_analysis("a", _analysis(), tmp_path / "analysis.json")
    before = db.get_meeting("a")

    updated = db.update_meeting_info("a", "Release planning", [
        {"name": "Ahmad Khan", "context": "Will review the release.", "importance": "high"},
        {"name": "Sara", "context": "Owns QA.", "importance": "medium"},
    ])

    assert updated["title"] == "Release planning"
    assert [(p["name"], p["context"]) for p in updated["people"] if p["type"] == "mentioned"] == [
        ("Ahmad Khan", "Will review the release."), ("Sara", "Owns QA.")
    ]
    for field in ("started_at", "ended_at", "duration_seconds", "summary", "status"):
        assert updated[field] == before[field]
    for field in ("tasks", "requested_changes", "decisions", "goals", "key_topics", "next_steps"):
        assert updated[field] == before[field]


def test_meeting_info_edit_is_rejected_until_analysis_completes(tmp_path):
    db = Database(tmp_path / "test.db")
    db.create_meeting("a", "Still recording", "2026-08-10T10:00:00+05:00", tmp_path / "a.wav")
    try:
        db.update_meeting_info("a", "Too early", [])
        assert False, "Expected incomplete meeting edit to fail"
    except ValueError as exc:
        assert "only be edited after analysis is completed" in str(exc)


def test_manual_meeting_info_survives_reanalysis(tmp_path):
    db = Database(tmp_path / "test.db")
    db.create_meeting("a", "Initial title", "2026-08-10T10:00:00+05:00", tmp_path / "a.wav")
    db.save_analysis("a", _analysis(), tmp_path / "analysis-1.json")
    db.update_meeting_info("a", "My corrected title", [
        {"name": "Correct Name", "context": "Corrected by the user.", "importance": "medium"}
    ])

    db.save_analysis("a", _analysis(title="New AI title", summary="Improved summary", person="Wrong Name"),
                     tmp_path / "analysis-2.json")
    meeting = db.get_meeting("a")
    assert meeting["title"] == "My corrected title"
    assert meeting["summary"] == "Improved summary"
    assert [p["name"] for p in meeting["people"] if p["type"] == "mentioned"] == ["Correct Name"]


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
    service._queue = service._worker = None; service._pending = []
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


def test_cross_track_echo_removal_handles_different_whisper_boundaries():
    system = [{
        "start": 10.0, "end": 41.0, "speaker": "Other participant",
        "text": "Those were some things and some feedback. Let us fix these logos and make that a priority because everything needs to look good."
    }]
    microphone = [
        {"start": 10.0, "end": 20.0, "speaker": "Hussain",
         "text": "Those were some things and some feedback."},
        {"start": 24.0, "end": 34.0, "speaker": "Hussain",
         "text": "Let us fix these logos and make that a priority."},
    ]

    kept, removed = TranscriptionService._remove_microphone_echoes(system, microphone)

    assert kept == []
    assert removed == 2


def test_cross_track_echo_removal_tolerates_recognition_errors_but_keeps_unique_speech():
    system = [{"start": 0.0, "end": 12.0, "speaker": "Other participant",
               "text": "Robert can get access to private APIs from different sports league management applications."}]
    microphone = [
        {"start": 1.0, "end": 10.0, "speaker": "Hussain",
         "text": "Robert can get access to private APIs from different sports lead management applications."},
        {"start": 5.0, "end": 9.0, "speaker": "Hussain",
         "text": "I will personally update the backend tomorrow."},
    ]

    kept, removed = TranscriptionService._remove_microphone_echoes(system, microphone)

    assert removed == 1
    assert [segment["text"] for segment in kept] == ["I will personally update the backend tomorrow."]


def test_short_simultaneous_backchannel_is_not_removed_as_echo():
    system = [{"start": 0.0, "end": 2.0, "speaker": "Other participant", "text": "Okay, sounds good."}]
    microphone = [{"start": 0.2, "end": 1.5, "speaker": "Hussain", "text": "Okay, sounds good."}]

    kept, removed = TranscriptionService._remove_microphone_echoes(system, microphone)

    assert kept == microphone
    assert removed == 0


def test_transcription_pipeline_reports_and_removes_cross_track_echo(tmp_path):
    class Segment:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    class Info:
        language = "en"

    class Model:
        def transcribe(self, path, **_kwargs):
            if path.endswith("recording-system.wav"):
                return [Segment(0, 12, "Please fix the logos before the product demonstration tomorrow.")], Info()
            return [
                Segment(1, 10, "Please fix the logos before the product demonstration tomorrow."),
                Segment(13, 18, "I will update the backend this afternoon."),
            ], Info()

    (tmp_path / "recording-system.wav").write_bytes(b"system")
    (tmp_path / "recording-microphone.wav").write_bytes(b"microphone")
    service = TranscriptionService(user_name="Hussain", backend="faster", task="translate")
    service._model = Model()

    result = service.transcribe(tmp_path / "recording.wav")

    assert [(segment["speaker"], segment["text"]) for segment in result["segments"]] == [
        ("Other participant", "Please fix the logos before the product demonstration tomorrow."),
        ("Hussain", "I will update the backend this afternoon."),
    ]
    assert result["quality"]["removed_cross_track_echoes"] == 1
    assert json.loads((tmp_path / "transcript.json").read_text())["segments"] == result["segments"]


def test_schema_and_chunking():
    payload={"meeting":{"title":"Test","date":"2026-08-10","start_time":"2026-08-10T10:00:00+05:00","end_time":"2026-08-10T10:10:00+05:00","duration_minutes":10},"summary":"A concise summary.","tasks":[],"goals":[],"key_topics":[],"decisions":[],"attendees":[],"people_mentioned":[],"next_steps":[]}
    assert MeetingAnalysis.model_validate(payload).meeting.title == "Test"
    assert "open_questions" not in MeetingAnalysis.model_json_schema()["properties"]
    assert len(chunk_transcript("x"*30000, 10000, 100)) > 1


def test_all_analysis_prompts_resolve_required_placeholders():
    from backend.analysis.prompts import ACTION_AUDIT_PROMPT, CHUNK_PROMPT, FINAL_PROMPT
    from backend.analysis.ollama import CHUNK_JSON_SCHEMA, LLMService

    service = LLMService("http://127.0.0.1:11434", "test", "Hussain")
    assert "{user_name}" not in service._system_prompt("2026-08-10")
    chunk = CHUNK_PROMPT.format(transcript="Hussain: I will prepare the report.", chunk_id="chunk_001", user_name="Hussain")
    assert "{user_name}" not in chunk
    action = ACTION_AUDIT_PROMPT.format(transcript="Hussain: I will prepare the report.", chunk_id="chunk_001", user_name="Hussain")
    assert "{user_name}" not in action
    final = FINAL_PROMPT.format(metadata="{}", schema="{}", candidates="[]", source_transcript="null", meeting_date="2026-08-10", user_name="Hussain")
    assert "{meeting_date}" not in final
    assert CHUNK_JSON_SCHEMA["properties"]["tasks"]["items"]["properties"]["confidence"]["type"] == "string"
    assert "requested_changes" in CHUNK_JSON_SCHEMA["required"]


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


def test_llm_preserves_hussain_assignments_and_requested_changes_with_evidence():
    from backend.analysis.ollama import LLMService
    metadata={"title":"Product review","date":"2026-08-10","start_time":"2026-08-10T10:00:00+05:00","end_time":"2026-08-10T10:10:00+05:00","duration_minutes":10}
    transcript="""Other participant: Ahmed joined the meeting earlier.
Other participant: The product presentation is important.
Other participant: We reviewed several screens.
Other participant: Hussain, please fix the logos before the demo.
Husain: I will update the backend this afternoon.
Other participant: Please deploy the backend later."""
    raw={"summary":"The product screens and demo readiness were reviewed.","goals":[],"key_topics":[],"decisions":[],"attendees":[],"people_mentioned":[],"next_steps":[],
         "tasks":[
             {"owner":"Hussain","task":"Fix the logos before the demo","priority":"high","confidence":"high","evidence":"Hussain, please fix the logos before the demo."},
             {"owner":"Husain","task":"Update the backend this afternoon","priority":"medium","confidence":"high","evidence":"I will update the backend this afternoon."},
             {"owner":"Ahmed","task":"Deploy the backend later","priority":"medium","confidence":"low","evidence":"Please deploy the backend later."}],
         "requested_changes":[{"requested_of":"Hussain","change":"Fix the logos before the demo","priority":"high","confidence":"high","evidence":"Hussain, please fix the logos before the demo."}]}
    service=LLMService("http://127.0.0.1:11434","test","Hussain",["Husain"])
    result=MeetingAnalysis.model_validate(service._sanitize(raw,transcript,metadata))
    assert [task.owner for task in result.tasks] == ["Hussain", "Hussain"]
    assert result.requested_changes[0].requested_of == "Hussain"
    assert result.requested_changes[0].change == "Fix the logos before the demo"


def test_llm_recovers_minor_evidence_wording_changes_from_actual_transcript():
    from backend.analysis.ollama import _find_source_quote
    transcript = "Other participant: Robert can access private APIs from different sports league management applications."
    recovered = _find_source_quote(
        "Robert can access private APIs from different sport lead management applications.", transcript
    )
    assert recovered == "Robert can access private APIs from different sports league management applications."


def test_analysis_runs_a_dedicated_action_audit_and_passes_it_to_final_synthesis():
    from backend.analysis.ollama import ACTION_JSON_SCHEMA, CHUNK_JSON_SCHEMA, LLMService

    metadata={"title":"Product review","date":"2026-08-10","start_time":"2026-08-10T10:00:00+05:00","end_time":"2026-08-10T10:10:00+05:00","duration_minutes":10}
    transcript="Hussain: I will update the scorekeeper application this afternoon."
    task={"owner":"Hussain","action":"Update the scorekeeper application","task":"Update the scorekeeper application",
          "deadline":None,"original_deadline_phrase":None,"priority":"medium","status":"open",
          "confidence":"high","evidence":"I will update the scorekeeper application this afternoon.","source_chunk_id":"ignored"}
    service=LLMService("http://127.0.0.1:11434","test","Hussain")
    service.availability=lambda:{"available":True,"model_installed":True}
    schemas=[]

    def generate(prompt, *, schema=None, meeting_date=None):
        schemas.append(schema)
        if schema is CHUNK_JSON_SCHEMA:
            return {key:[] for key in CHUNK_JSON_SCHEMA["required"]}
        if schema is ACTION_JSON_SCHEMA:
            return {"tasks":[dict(task)],"requested_changes":[]}
        assert "Update the scorekeeper application" in prompt
        return {"meeting":metadata,"summary":"Hussain committed to updating the scorekeeper application.",
                "goals":[],"key_topics":[],"decisions":[],"attendees":[],"people_mentioned":[],
                "tasks":[dict(task)],"requested_changes":[],"next_steps":[]}

    service._generate_json=generate
    result=service.analyze(transcript,metadata)

    assert CHUNK_JSON_SCHEMA in schemas
    assert ACTION_JSON_SCHEMA in schemas
    assert result.tasks[0].owner == "Hussain"
    assert result.tasks[0].task == "Update the scorekeeper application"


def test_action_duplicates_merge_by_verified_evidence_and_keep_deadline():
    from backend.analysis.ollama import LLMService
    metadata={"title":"Demo","date":"2026-08-10","start_time":"2026-08-10T10:00:00+05:00","end_time":"2026-08-10T10:10:00+05:00","duration_minutes":10}
    transcript="Other participant: Hussain, kindly fix the placeholder logos before the client demo."
    raw={"summary":"The client demo visuals were reviewed.","goals":[],"key_topics":[],"decisions":[],"attendees":[],"people_mentioned":[],"next_steps":[],
         "tasks":[
             {"owner":"Hussain","task":"Fix placeholder logos before the client demo","priority":"medium","confidence":"high","evidence":"Other participant: Hussain, kindly fix the placeholder logos before the client demo."},
             {"owner":"Hussain","action":"Fix","task":"the placeholder logos before the client demo","deadline":"before the client demo","priority":"medium","confidence":"high","evidence":"Hussain, kindly fix the placeholder logos before the client demo."}],
         "requested_changes":[
             {"requested_of":"Hussain","change":"Fix placeholder logos before the client demo","priority":"medium","confidence":"high","evidence":"Other participant: Hussain, kindly fix the placeholder logos before the client demo."},
             {"requested_of":"Hussain","change":"Fix the placeholder logos before the client demo","deadline":"before the client demo","priority":"medium","confidence":"high","evidence":"Hussain, kindly fix the placeholder logos before the client demo."}]}
    service=LLMService("http://127.0.0.1:11434","test","Hussain")
    result=MeetingAnalysis.model_validate(service._sanitize(raw,transcript,metadata))
    assert len(result.tasks) == 1
    assert result.tasks[0].deadline.original == "before the client demo"
    assert len(result.requested_changes) == 1
    assert result.requested_changes[0].deadline.original == "before the client demo"


def test_pdf_report_builds_from_latest_saved_meeting(tmp_path):
    from urllib.parse import quote
    import arabic_reshaper
    from bidi.algorithm import get_display
    from backend.reports.pdf import (
        _display_text,
        _friendly_owner,
        build_meeting_report,
        content_disposition,
        report_filename,
    )

    db = Database(tmp_path / "test.db")
    db.create_meeting(
        "pdf", "Product <launch>\nreport", "2026-08-10T10:00:00+05:00", tmp_path / "meeting.wav"
    )
    db.finish_recording("pdf", "2026-08-10T10:18:00+05:00", 1080)
    db.save_analysis("pdf", _analysis(), tmp_path / "analysis.json")
    db.update_meeting_info("pdf", "Client launch & delivery", [
        {"name": "علی", "context": "Will review the release & delivery plan.", "importance": "high"}
    ])
    task_id = db.get_meeting("pdf")["tasks"][0]["id"]
    db.update_task(task_id, "completed")

    document = build_meeting_report(db.get_meeting("pdf"), "Hussain", ["Husain"])
    filename = report_filename("Client launch & delivery\r\nInjected", "2026-08-10T10:00:00+05:00")
    unicode_filename = report_filename("میٹنگ جائزہ", "2026-08-10T10:00:00+05:00")

    assert document.startswith(b"%PDF-")
    assert document.rstrip().endswith(b"%%EOF")
    assert len(document) > 5_000
    assert filename == "client-launch-delivery-injected-2026-08-10-report.pdf"
    assert "\r" not in content_disposition(filename)
    assert "\n" not in content_disposition(filename)
    assert unicode_filename.startswith("میٹنگ-جائزہ-")
    assert quote(unicode_filename) in content_disposition(unicode_filename)
    assert _display_text("علی رضا") == get_display(arabic_reshaper.reshape("علی رضا"))
    assert _friendly_owner("unknown_participant") == "Participant not identified"
    assert _friendly_owner("team") == "Team"


def test_pdf_report_endpoint_is_completed_only(tmp_path):
    from fastapi import FastAPI
    from backend.api.routes import create_router

    db = Database(tmp_path / "test.db")
    db.create_meeting("ready", "Weekly review", "2026-08-10T10:00:00+05:00", tmp_path / "ready.wav")
    db.finish_recording("ready", "2026-08-10T10:10:00+05:00", 600)
    db.save_analysis("ready", _analysis(), tmp_path / "analysis.json")
    db.update_meeting_info("ready", "Weekly review", [])
    db.create_meeting("active", "Active meeting", "2026-08-10T11:00:00+05:00", tmp_path / "active.wav")

    app = FastAPI()
    app.include_router(create_router(db, object()))
    client = TestClient(app)

    response = client.get("/api/meetings/ready/report.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert "weekly-review-2026-08-10-report.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/meetings/active/report.pdf").status_code == 409
    assert client.get("/api/meetings/missing/report.pdf").status_code == 404


def test_health(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api.db"))
    from backend.main import app
    assert TestClient(app).get("/api/health").json()["status"] == "ok"


def _run_monitor_with_readings(readings, end_confirmations):
    """Drive MeetDetector.monitor through a scripted sequence of detect() results."""
    from backend.detection.meet_detector import DetectionError, MeetDetector, MeetTab
    detector = MeetDetector(end_confirmations=end_confirmations)
    events = []
    script = iter(readings)

    class ScriptDone(Exception): pass

    def fake_detect():
        value = next(script, ScriptDone())
        if isinstance(value, Exception): raise value
        return MeetTab("Meet", value) if value else None

    async def on_detected(tab): events.append(("detected", tab.url))
    async def on_ended(tab): events.append(("ended", tab.url))

    async def drive():
        detector.detect = fake_detect
        try: await detector.monitor(on_detected, on_ended, interval=0)
        except ScriptDone: pass
    asyncio.run(drive())
    return events


def test_detector_ignores_transient_misses_and_errors():
    from backend.detection.meet_detector import DetectionError
    url = "https://meet.google.com/abc-defg-hij"
    readings = [url, None, DetectionError("timeout"), None, DetectionError("osascript failed"), url, None, None, url]
    assert _run_monitor_with_readings(readings, end_confirmations=3) == [("detected", url)]


def test_detector_ends_meeting_only_after_consecutive_confirmations():
    url = "https://meet.google.com/abc-defg-hij"
    readings = [url, None, None, None, None, url]
    events = _run_monitor_with_readings(readings, end_confirmations=3)
    assert events == [("detected", url), ("ended", url), ("detected", url)]


def test_detector_parses_osascript_output(monkeypatch):
    import subprocess
    from backend.detection.meet_detector import BrowserTabProvider, DetectionError, MeetDetector
    import pytest
    detector = MeetDetector(providers=[BrowserTabProvider("Google Chrome")])
    outputs = {}
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, outputs["code"], outputs["out"], outputs.get("err", ""))
    monkeypatch.setattr(subprocess, "run", fake_run)
    outputs.update(code=0, out="MEET\thttps://meet.google.com/abc-defg-hij\tMeet – abc\n")
    tab = detector.detect(); assert (tab.url, tab.title, tab.platform) == ("https://meet.google.com/abc-defg-hij", "Meet – abc", "google_meet")
    outputs.update(code=0, out="MEET\thttps://meet.google.com/abc-defg-hij\t\n")
    assert detector.detect().title == "Google Meet"  # empty title must not read as "no meeting"
    outputs.update(code=0, out="MEET\thttps://us05web.zoom.us/j/123456?pwd=x\tZoom Meeting\n")
    assert detector.detect().platform == "zoom"
    outputs.update(code=0, out="MEET\thttps://teams.microsoft.com/v2/\tMeeting | Microsoft Teams\n")
    assert detector.detect().platform == "teams"
    outputs.update(code=0, out="NONE\n")
    assert detector.detect() is None
    outputs.update(code=1, out="", err="execution error: Google Chrome got an error")
    with pytest.raises(DetectionError): detector.detect()
    def timeout_run(*args, **kwargs): raise subprocess.TimeoutExpired("osascript", 15)
    monkeypatch.setattr(subprocess, "run", timeout_run)
    with pytest.raises(DetectionError): detector.detect()


def test_browser_script_uses_explicit_delimiter_not_tab_keyword():
    # Inside `tell application "Google Chrome"` the word `tab` is Chrome's tab class, not a character.
    from backend.detection.meet_detector import BrowserTabProvider, TeamsAppProvider
    assert "& tab &" not in BrowserTabProvider("Google Chrome").script
    assert "ASCII character 9" in BrowserTabProvider("Google Chrome").script
    assert "& tab &" not in TeamsAppProvider.SCRIPT


def test_detector_combines_providers_and_tolerates_partial_failures():
    from backend.detection.meet_detector import DetectionError, MeetDetector, MeetTab
    import pytest
    class Broken:
        def detect(self, timeout): raise DetectionError("no accessibility permission")
    class Quiet:
        def detect(self, timeout): return None
    class Zoom:
        def detect(self, timeout): return MeetTab("Zoom meeting", "zoom://meeting", "zoom")
    assert MeetDetector(providers=[Broken(), Quiet(), Zoom()]).detect().platform == "zoom"
    assert MeetDetector(providers=[Broken(), Quiet()]).detect() is None   # one working provider is enough for "no meeting"
    with pytest.raises(DetectionError): MeetDetector(providers=[Broken(), Broken()]).detect()


def test_recorder_health_reports_dead_capture_process(tmp_path):
    import subprocess
    recorder = AudioRecorder()
    assert recorder.health() is None
    recorder.process = subprocess.Popen(["sh", "-c", "echo 'Capture stopped: display went away' >&2; exit 3"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    recorder._drain_stderr(recorder.process)
    recorder.started_monotonic = time.monotonic()
    recorder.process.wait(timeout=5); time.sleep(0.2)
    assert "display went away" in recorder.health()


def test_processing_waits_until_active_recording_stops(monkeypatch, tmp_path):
    import backend.meetings.service as meeting_service_module
    db = Database(tmp_path / "test.db")
    audio = tmp_path / "a" / "recording.wav"; audio.parent.mkdir(); audio.write_bytes(b"audio")
    db.create_meeting("a", "a", "2026-08-10T10:00:00+05:00", audio)
    service = MeetingService.__new__(MeetingService); service.db = db
    service._queue = service._worker = None; service._pending = []
    service.settings = type("S", (), {"defer_processing_while_recording": True})()
    machine = StateMachine(); monkeypatch.setattr(meeting_service_module, "runtime", machine)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(meeting_service_module.asyncio, "sleep", lambda *_: real_sleep(0))
    started = []

    async def fake_process(meeting_id, prefer_saved_transcript=False, analyze=True):
        started.append(machine.snapshot()["recording"])
        machine.processing_transition(MeetingState.TRANSCRIBING, meeting_id=meeting_id)
        machine.processing_transition(MeetingState.ANALYZING); machine.processing_transition(MeetingState.COMPLETED)
    service.process = fake_process

    async def scenario():
        machine.transition(MeetingState.RECORDING, recording=True, meeting_id="live")
        service.enqueue("a")
        for _ in range(20): await asyncio.sleep(0)
        assert started == []  # still waiting while the recording runs
        assert "Waiting for the current recording" in machine.snapshot()["processing"]["stage_detail"]
        machine.transition(MeetingState.IDLE, recording=False, meeting_id=None)
        await service._queue.join()
    asyncio.run(scenario())
    assert started == [False]


def _service_for_delete(monkeypatch, tmp_path):
    import backend.meetings.service as meeting_service_module
    db = Database(tmp_path / "test.db")
    recordings = tmp_path / "meetings"; recordings.mkdir()
    service = MeetingService.__new__(MeetingService); service.db = db
    service._queue = service._worker = None; service._pending = []
    service.settings = type("S", (), {"recordings_path": recordings, "defer_processing_while_recording": True})()
    machine = StateMachine(); monkeypatch.setattr(meeting_service_module, "runtime", machine)
    return db, service, machine, recordings


def test_delete_meeting_removes_rows_and_files(monkeypatch, tmp_path):
    db, service, machine, recordings = _service_for_delete(monkeypatch, tmp_path)
    folder = recordings / "done"; folder.mkdir()
    for name in ("recording.wav", "recording-system.wav", "transcript.json", "analysis.json"):
        (folder / name).write_bytes(b"data")
    db.create_meeting("done", "Finished", "2026-08-10T10:00:00+05:00", folder / "recording.wav")
    db.finish_recording("done", "2026-08-10T10:10:00+05:00", 600)
    db.save_transcript("done", [{"start": 0, "end": 1, "speaker": "Hussain", "text": "hello"}], folder / "transcript.json")
    db.save_analysis("done", _analysis(), folder / "analysis.json")
    assert db.tasks()

    result = service.delete_meeting("done")
    assert result == {"deleted": "done", "files_removed": True}
    assert db.get_meeting("done") is None
    assert not folder.exists() and recordings.exists()
    assert db.tasks() == [] and db.transcript("done") == []
    with db.connection() as conn:
        for table in ("people", "requested_changes", "decisions", "goals", "key_topics", "next_steps", "meeting_info_edits"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE meeting_id='done'").fetchone()[0] == 0


def test_delete_refuses_active_recording_processing_and_queued(monkeypatch, tmp_path):
    import pytest
    db, service, machine, recordings = _service_for_delete(monkeypatch, tmp_path)
    for meeting_id in ("live", "busy", "waiting"):
        folder = recordings / meeting_id; folder.mkdir(); (folder / "recording.wav").write_bytes(b"data")
        db.create_meeting(meeting_id, meeting_id, "2026-08-10T10:00:00+05:00", folder / "recording.wav")
    machine.transition(MeetingState.RECORDING, recording=True, meeting_id="live")
    machine.processing_transition(MeetingState.RECORDED, meeting_id="busy"); machine.processing_transition(MeetingState.TRANSCRIBING)
    service._pending = ["waiting"]
    for meeting_id in ("live", "busy", "waiting"):
        with pytest.raises(ValueError): service.delete_meeting(meeting_id)
        assert db.get_meeting(meeting_id) and (recordings / meeting_id / "recording.wav").exists()
    with pytest.raises(KeyError): service.delete_meeting("missing")


def test_delete_never_touches_folders_outside_recordings_root(monkeypatch, tmp_path):
    db, service, machine, recordings = _service_for_delete(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere"; outside.mkdir(); (outside / "recording.wav").write_bytes(b"keep")
    db.create_meeting("ext", "External", "2026-08-10T10:00:00+05:00", outside / "recording.wav")
    assert service.delete_meeting("ext") == {"deleted": "ext", "files_removed": False}
    assert (outside / "recording.wav").exists() and db.get_meeting("ext") is None


def test_delete_endpoint_maps_errors(tmp_path):
    from fastapi import FastAPI
    from backend.api.routes import create_router

    class FakeService:
        def delete_meeting(self, meeting_id):
            if meeting_id == "missing": raise KeyError("Meeting not found.")
            if meeting_id == "busy": raise ValueError("This meeting is being processed.")
            return {"deleted": meeting_id, "files_removed": True}

    app = FastAPI(); app.include_router(create_router(Database(tmp_path / "test.db"), FakeService()))
    client = TestClient(app)
    assert client.delete("/api/meetings/ok").json() == {"deleted": "ok", "files_removed": True}
    assert client.delete("/api/meetings/missing").status_code == 404
    assert client.delete("/api/meetings/busy").status_code == 409


# ---------------------------------------------------------------- transcription source of truth


def test_translation_is_attached_without_touching_original_text():
    original = [{"start": 0.0, "end": 4.0, "text": "ہم کل ریلیز کریں گے"}, {"start": 4.5, "end": 8.0, "text": "ٹھیک ہے"}]
    english = [{"start": 0.2, "end": 3.8, "text": "We will release tomorrow"}, {"start": 4.6, "end": 7.9, "text": "Okay"}]
    TranscriptionService._attach_translation(original, english)
    assert original[0]["text"] == "ہم کل ریلیز کریں گے" and original[0]["text_en"] == "We will release tomorrow"
    assert original[1]["text_en"] == "Okay"


def test_english_meeting_skips_translation_pass(monkeypatch):
    service = TranscriptionService(user_name="Hussain", backend="faster", task="transcribe", translation="auto")
    calls = []
    def fake_decode(path, task):
        calls.append(task)
        return [{"start": 0.0, "end": 2.0, "text": "Ship it.", "avg_logprob": -0.1, "no_speech_prob": 0.0}], "en"
    monkeypatch.setattr(service, "_decode_faster", fake_decode)
    segments, language, output = service._transcribe_track(Path("x.wav"))
    assert calls == ["transcribe"] and segments[0]["text_en"] == "Ship it." and output == "en"


def test_non_english_meeting_gets_separate_english_pass(monkeypatch):
    service = TranscriptionService(user_name="Hussain", backend="faster", task="transcribe", translation="auto")
    calls = []
    def fake_decode(path, task):
        calls.append(task)
        if task == "transcribe":
            return [{"start": 0.0, "end": 2.0, "text": "کام ہو گیا", "avg_logprob": -0.2, "no_speech_prob": 0.0}], "ur"
        return [{"start": 0.1, "end": 1.9, "text": "The work is done", "avg_logprob": -0.2, "no_speech_prob": 0.0}], "ur"
    monkeypatch.setattr(service, "_decode_faster", fake_decode)
    segments, language, output = service._transcribe_track(Path("x.wav"))
    assert calls == ["transcribe", "translate"]
    assert segments[0]["text"] == "کام ہو گیا" and segments[0]["text_en"] == "The work is done" and language == "ur"


def test_transcript_text_for_analysis_prefers_english_rendering():
    text = MeetingService._transcript_text([{"speaker": "Speaker 1", "text": "کام ہو گیا", "text_en": "The work is done"},
                                            {"speaker": "Hussain", "text": "Great."}])
    assert text == "Speaker 1: The work is done\nHussain: Great."


# ---------------------------------------------------------------- diarization and naming


class _FakeExtractor:
    """Voice A lives in [1,0], voice B in [0,1]; the segment time decides which voice it is."""
    def __init__(self, voice_of): self.voice_of = voice_of
    def embed(self, samples):
        key = round(len(samples) / 16000, 1)
        return self.voice_of(key)


def _diarizer_with(db, tmp_path, monkeypatch, plan):
    from backend.transcription import diarize
    import numpy as np
    monkeypatch.setattr(diarize, "decode_audio", lambda *a, **k: np.zeros(16000 * 60, dtype=np.float32), raising=False)
    import faster_whisper.audio
    monkeypatch.setattr(faster_whisper.audio, "decode_audio", lambda *a, **k: np.zeros(16000 * 60, dtype=np.float32))
    return diarize.SpeakerDiarizer(db, threshold=0.5, match_threshold=0.7, extractor=_FakeExtractor(plan))


def test_diarizer_clusters_voices_and_short_segments_inherit_neighbours(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    # durations encode the voice: 5s segments are voice A, 6s segments are voice B
    plan = lambda seconds: [1.0, 0.02] if seconds == 5.0 else [0.02, 1.0]
    diarizer = _diarizer_with(db, tmp_path, monkeypatch, plan)
    segments = [
        {"start": 0.0, "end": 5.0, "text": "a1"}, {"start": 5.0, "end": 5.5, "text": "short"},
        {"start": 10.0, "end": 16.0, "text": "b1"}, {"start": 20.0, "end": 25.0, "text": "a2"},
        {"start": 30.0, "end": 36.0, "text": "b2"}, {"start": 40.0, "end": 46.0, "text": "b3"},
    ]
    summary = diarizer.label(tmp_path / "recording-system.wav", segments)
    assert [s["speaker"] for s in segments] == ["Speaker 2", "Speaker 2", "Speaker 1", "Speaker 2", "Speaker 1", "Speaker 1"]
    assert summary["spk_1"]["seconds"] == 18.0 and summary["spk_1"]["name"] is None and len(summary["spk_1"]["centroid"]) == 2


def test_diarizer_folds_noise_voices_into_nearest_real_voice(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    plan = lambda seconds: [1.0, 0.02] if seconds == 5.0 else [0.6, 0.8]   # the 2s voice is close to A but separate
    diarizer = _diarizer_with(db, tmp_path, monkeypatch, plan)
    segments = [{"start": 0.0, "end": 5.0, "text": "a1"}, {"start": 10.0, "end": 15.0, "text": "a2"}, {"start": 20.0, "end": 22.0, "text": "blip"}]
    summary = diarizer.label(tmp_path / "recording-system.wav", segments)
    assert len(summary) == 1 and [s["speaker"] for s in segments] == ["Speaker 1"] * 3


def test_named_voice_is_recognised_in_later_meetings(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    plan = lambda seconds: [1.0, 0.02] if seconds == 10.0 else [0.02, 1.0]
    diarizer = _diarizer_with(db, tmp_path, monkeypatch, plan)
    diarizer.remember("Ali", [1.0, 0.0])
    segments = [{"start": 0.0, "end": 10.0, "text": "a"}, {"start": 15.0, "end": 24.0, "text": "b"}]
    summary = diarizer.label(tmp_path / "recording-system.wav", segments)
    assert segments[0]["speaker"] == "Ali" and segments[1]["speaker"].startswith("Speaker ")
    matched = [v for v in summary.values() if v["name"] == "Ali"][0]
    assert matched["similarity"] >= 0.7
    assert db.upsert_voice_profile("Ali", [0.9, 0.1])["samples"] == 2  # repeated naming refines the centroid


def test_name_speaker_updates_rows_files_and_remembers_voice(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    folder = tmp_path / "m"; folder.mkdir()
    audio = folder / "recording.wav"; audio.write_bytes(b"x")
    db.create_meeting("m", "Meeting", "2026-08-10T10:00:00+05:00", audio)
    segments = [{"start": 0, "end": 1, "speaker": "Speaker 1", "speaker_id": "spk_1", "text": "hello", "text_en": "hello"},
                {"start": 1, "end": 2, "speaker": "Speaker 2", "speaker_id": "spk_2", "text": "hi", "text_en": "hi"}]
    transcript_path = folder / "transcript.json"
    transcript_path.write_text(json.dumps({"segments": segments, "speakers": {"spk_1": {"label": "Speaker 1", "name": None, "centroid": [1.0, 0.0]}}}))
    db.save_transcript("m", segments, transcript_path)
    service = MeetingService.__new__(MeetingService); service.db = db
    service.settings = type("S", (), {"user_name": "Hussain"})()
    class Remember:
        def remember(self, name, centroid): return db.upsert_voice_profile(name, centroid)
    service.diarizer = Remember()
    result = service.name_speaker("m", "Ali", speaker_id="spk_1", remember=True)
    assert result["renamed_lines"] == 1 and result["remembered"] is True
    assert [s["speaker"] for s in db.transcript("m")] == ["Ali", "Speaker 2"]
    saved = json.loads(transcript_path.read_text())
    assert saved["speakers"]["spk_1"]["name"] == "Ali" and saved["segments"][0]["speaker"] == "Ali"
    assert (folder / "transcript.txt").read_text().splitlines()[0] == "Ali: hello"
    assert db.voice_profiles()[0]["name"] == "Ali"
    assert service._speaker_context(db.transcript("m")) == {"user": "Hussain", "named": ["Ali"], "unnamed": ["Speaker 2"]}


def test_segment_edit_marks_row_and_regenerates_transcript_files(tmp_path):
    db = Database(tmp_path / "test.db")
    folder = tmp_path / "m"; folder.mkdir()
    audio = folder / "recording.wav"; audio.write_bytes(b"x")
    db.create_meeting("m", "Meeting", "2026-08-10T10:00:00+05:00", audio)
    transcript_path = folder / "transcript.json"
    transcript_path.write_text(json.dumps({"segments": []}))
    db.save_transcript("m", [{"start": 0, "end": 1, "speaker": "Speaker 1", "text": "fix the logs", "text_en": "fix the logs"}], transcript_path)
    service = MeetingService.__new__(MeetingService); service.db = db
    segment_id = db.transcript("m")[0]["id"]
    row = service.edit_segment("m", segment_id, text="fix the logos")
    assert row["text"] == "fix the logos" and row["text_en"] == "fix the logos" and row["edited"] == 1
    assert json.loads(transcript_path.read_text())["edited"] is True
    assert (folder / "transcript.txt").read_text() == "Speaker 1: fix the logos"


# ---------------------------------------------------------------- evidence to audio, templates


def test_evidence_locates_segment_and_spans_neighbours():
    from backend.analysis.evidence import locate_evidence
    segments = [{"id": 1, "start": 0.0, "end": 5.0, "speaker": "Ali", "text": "Please fix the logos before"},
                {"id": 2, "start": 5.0, "end": 9.0, "speaker": "Ali", "text": "the demo tomorrow."},
                {"id": 3, "start": 20.0, "end": 25.0, "speaker": "Hussain", "text": "I will update the backend."}]
    assert locate_evidence("I will update the backend", segments) == {"segment_id": 3, "start": 20.0, "end": 25.0, "speaker": "Hussain"}
    assert locate_evidence("fix the logos before the demo tomorrow", segments) == {"segment_id": 1, "start": 0.0, "end": 9.0, "speaker": "Ali"}
    assert locate_evidence("something never said in this meeting at all", segments) is None


def test_templates_and_speaker_legend_reach_the_system_prompt():
    from backend.analysis.ollama import LLMService
    from backend.analysis.templates import get_template, list_templates
    assert get_template("nonsense").key == "engineering" and any(t["default"] for t in list_templates())
    service = LLMService("http://127.0.0.1:11434", "test", "Hussain")
    service._meeting_context = {"template": "client", "speakers": {"named": ["Ali"], "unnamed": ["Speaker 2"]}}
    prompt = service._system_prompt("2026-08-10")
    assert "MEETING TYPE: CLIENT / STAKEHOLDER CALL" in prompt and "Confirmed people: Ali" in prompt and "Unnamed voices: Speaker 2" in prompt
    assert "{" not in prompt.split("SPEAKER LABELS")[1]


# ---------------------------------------------------------------- transcript export


def test_transcript_export_formats(tmp_path):
    from backend.transcription.export import transcript_as_json, transcript_as_srt, transcript_as_text, transcript_filename
    meeting = {"id": "m", "title": "Weekly <sync>", "started_at": "2026-09-10T17:22:43+05:00", "duration_seconds": 2482}
    segments = [
        {"id": 1, "start": 0.0, "end": 4.2, "speaker": "Ali", "text": "کام ہو گیا", "text_en": "The work is done"},
        {"id": 2, "start": 65.5, "end": 70.0, "speaker": "Hussain", "text": "Great, ship it.", "text_en": "Great, ship it."},
    ]
    text = transcript_as_text(meeting, segments)
    assert text.startswith("Weekly <sync>\n10 September 2026, 17:22\nDuration: 41 minutes\n")
    assert "[0:00] Ali: کام ہو گیا\n    (The work is done)\n[1:05] Hussain: Great, ship it.\n" in text
    assert "(Great, ship it.)" not in text                       # identical English is not repeated
    assert "Ali: The work is done" in transcript_as_text(meeting, segments, language="english")
    assert transcript_as_text(meeting, segments, language="original", timestamps=False).endswith("Ali: کام ہو گیا\nHussain: Great, ship it.\n")
    srt = transcript_as_srt(segments)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:04,200\nAli: کام ہو گیا\n\n2\n00:01:05,500 --> 00:01:10,000\nHussain: Great, ship it.\n")
    payload = json.loads(transcript_as_json(meeting, segments, {"spk_1": {"label": "Ali", "centroid": [1, 2]}}))
    assert payload["meeting"]["title"] == "Weekly <sync>" and payload["segments"][0]["text_en"] == "The work is done"
    assert "centroid" not in payload["speakers"]["spk_1"]      # voice embeddings never leave the app
    assert transcript_filename("Weekly <sync>", "2026-09-10T17:22:43+05:00", "txt") == "weekly-sync-2026-09-10-transcript.txt"


def test_transcript_export_endpoint(tmp_path):
    from fastapi import FastAPI
    from backend.api.routes import create_router
    db = Database(tmp_path / "test.db")
    folder = tmp_path / "m"; folder.mkdir()
    db.create_meeting("m", "Planning", "2026-09-10T17:22:43+05:00", folder / "recording.wav")
    db.create_meeting("empty", "Silent", "2026-09-10T18:00:00+05:00", folder / "other.wav")
    db.save_transcript("m", [{"start": 0, "end": 1, "speaker": "Ali", "text": "hello", "text_en": "hello"}], folder / "transcript.json")
    app = FastAPI(); app.include_router(create_router(db, object()))
    client = TestClient(app)
    response = client.get("/api/meetings/m/transcript/export")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    assert 'filename="planning-2026-09-10-transcript.txt"' in response.headers["content-disposition"]
    assert "[0:00] Ali: hello" in response.text
    inline = client.get("/api/meetings/m/transcript/export?download=false")
    assert "content-disposition" not in inline.headers and inline.text == response.text
    assert 'planning-2026-09-10-transcript.srt"' in client.get("/api/meetings/m/transcript/export?format=srt").headers["content-disposition"]
    assert client.get("/api/meetings/m/transcript/export?format=json").json()["segments"][0]["speaker"] == "Ali"
    assert client.get("/api/meetings/empty/transcript/export").status_code == 404
    assert client.get("/api/meetings/missing/transcript/export").status_code == 404
    assert client.get("/api/meetings/m/transcript/export?format=docx").status_code == 422


# ---------------------------------------------------------------- two-phase pipeline: transcribe, review, analyze


def _reviewable_service(monkeypatch, tmp_path, auto_analyze=False):
    import backend.meetings.service as meeting_service_module
    db = Database(tmp_path / "test.db")
    folder = tmp_path / "m"; folder.mkdir()
    audio = folder / "recording.wav"; audio.write_bytes(b"audio")
    db.create_meeting("m", "Planning", "2026-09-10T17:22:43+05:00", audio)
    db.finish_recording("m", "2026-09-10T18:00:00+05:00", 2237)
    service = MeetingService.__new__(MeetingService); service.db = db
    service._queue = service._worker = None; service._pending = []
    service.settings = type("S", (), {"auto_analyze": auto_analyze, "defer_processing_while_recording": True, "user_name": "Hussain"})()
    service.recorder = type("R", (), {"measure_audio": staticmethod(lambda path: {"has_audible_audio": True})})()
    transcript = {"segments": [{"start": 0, "end": 2, "speaker": "Speaker 1", "text": "Please fix the logos.", "text_en": "Please fix the logos."},
                               {"start": 2, "end": 4, "speaker": "Speaker 1", "text": "um yeah", "text_en": "um yeah"}], "quality": {"score": 96}}
    def fake_transcribe(path):
        (Path(path).parent / "transcript.json").write_text(json.dumps(transcript)); return transcript
    service.transcriber = type("T", (), {"transcribe": staticmethod(fake_transcribe)})()
    analyses = []
    class FakeLLM:
        def analyze(self, text, metadata, progress=None):
            analyses.append(text); return _analysis()
    service.llm = FakeLLM()
    machine = StateMachine(); monkeypatch.setattr(meeting_service_module, "runtime", machine)
    monkeypatch.setattr(meeting_service_module, "notify", lambda *a, **k: None)
    return db, service, machine, analyses


def test_recording_stops_at_transcript_until_the_user_analyzes(monkeypatch, tmp_path):
    db, service, machine, analyses = _reviewable_service(monkeypatch, tmp_path)
    asyncio.run(service.process("m", analyze=False))
    meeting = db.get_meeting("m")
    assert meeting["status"] == "transcribed" and analyses == [] and meeting["summary"] == ""
    assert machine.snapshot()["processing"]["state"] == "transcribed" and not machine.processing_busy()
    assert (tmp_path / "m" / "transcript.json").exists() and len(db.transcript("m")) == 2

    # the user deletes the filler line, then presses Analyze
    filler = db.transcript("m")[1]["id"]
    assert service.delete_segment("m", filler) == {"deleted": filler, "remaining": 1}
    assert (tmp_path / "m" / "transcript.txt").read_text() == "Speaker 1: Please fix the logos."
    scheduled = []
    import backend.meetings.service as meeting_service_module
    monkeypatch.setattr(meeting_service_module.asyncio, "create_task", lambda coro: (scheduled.append(coro), coro.close()))
    assert asyncio.run(service.analyze("m")) == "transcript"
    assert db.get_meeting("m")["status"] == "transcribing" and scheduled
    # and the analysis phase itself reads the corrected transcript, never Whisper again
    asyncio.run(service.process("m", prefer_saved_transcript=True, analyze=True))
    assert db.get_meeting("m")["status"] == "completed"
    assert analyses == ["Speaker 1: Please fix the logos."]


def test_auto_analyze_setting_keeps_the_single_step_flow(monkeypatch, tmp_path):
    db, service, machine, analyses = _reviewable_service(monkeypatch, tmp_path, auto_analyze=True)
    async def run():
        service.enqueue("m")
        await service._queue.join()
    asyncio.run(run())
    assert db.get_meeting("m")["status"] == "completed" and len(analyses) == 1


def test_analyze_refuses_without_transcript_or_while_busy(monkeypatch, tmp_path):
    import pytest
    db, service, machine, analyses = _reviewable_service(monkeypatch, tmp_path)
    with pytest.raises(ValueError): asyncio.run(service.analyze("m"))          # nothing transcribed yet
    db.set_status("m", "transcribing")
    with pytest.raises(ValueError): asyncio.run(service.analyze("m"))          # still processing
    with pytest.raises(KeyError): asyncio.run(service.analyze("missing"))
