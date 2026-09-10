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
    state = StateMachine(); state.transition(MeetingState.RECORDING); state.transition(MeetingState.RECORDED)
    state.transition(MeetingState.TRANSCRIBING); state.transition(MeetingState.ANALYZING); state.transition(MeetingState.COMPLETED)
    assert state.snapshot()["state"] == "completed"


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
    service = TranscriptionService(user_name="Hussain")
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
