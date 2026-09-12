import json
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
from typing import Literal
from pydantic import BaseModel, Field, field_validator
from backend.analysis.evidence import attach_evidence_locations
from backend.analysis.templates import list_templates
from backend.audio.devices import AudioDeviceManager
from backend.config import get_settings
from backend.reports.pdf import build_meeting_report, content_disposition, report_filename
from backend.state import runtime


log = logging.getLogger(__name__)


class StartRequest(BaseModel):
    title: str = "Untitled meeting"
    meet_url: str | None = None
    template: str | None = None
    platform: str | None = None


class SpeakerNameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    speaker_id: str | None = None
    current_label: str | None = None
    remember: bool = True


class SegmentEdit(BaseModel):
    text: str | None = Field(default=None, max_length=5000)
    speaker: str | None = Field(default=None, max_length=120)


class TaskUpdate(BaseModel):
    status: str


class RetryRequest(BaseModel):
    retranscribe: bool = False


class MentionedPersonUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    context: str = Field(default="", max_length=500)
    importance: Literal["high", "medium", "low"] = "medium"

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str):
        if not value.strip():
            raise ValueError("Name cannot be empty")
        return value.strip()


class MeetingInfoUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    people_mentioned: list[MentionedPersonUpdate] = Field(default_factory=list, max_length=100)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str):
        if not value.strip():
            raise ValueError("Meeting title cannot be empty")
        return value.strip()


def create_router(db, service):
    router = APIRouter(prefix="/api")
    settings = get_settings()
    user_aliases = {alias.casefold() for alias in settings.aliases}

    def add_user_ownership(value):
        if not value:
            return value
        for task in value.get("tasks", []):
            task["is_mine"] = str(task.get("owner") or "").casefold() in user_aliases
        for change in value.get("requested_changes", []):
            change["is_mine"] = str(change.get("requested_of") or "").casefold() in user_aliases
        return value

    @router.get("/health")
    def health(): return {"status": "ok", "local_only": True}

    @router.get("/status")
    def status(): return runtime.snapshot()

    @router.get("/meetings")
    def meetings(q: str | None = Query(None)): return db.list_meetings(q)

    def with_evidence_locations(value):
        segments = db.transcript(value["id"])
        attach_evidence_locations(value.get("tasks", []), segments)
        attach_evidence_locations(value.get("requested_changes", []), segments)
        return value

    @router.get("/meetings/{meeting_id}")
    def meeting(meeting_id: str):
        value = db.get_meeting(meeting_id)
        if not value: raise HTTPException(404, "Meeting not found")
        return with_evidence_locations(add_user_ownership(value))

    @router.get("/meetings/{meeting_id}/transcript")
    def transcript(meeting_id: str):
        value = db.get_meeting(meeting_id)
        if not value: raise HTTPException(404, "Meeting not found")
        speakers = {}
        try:
            path = Path(value.get("transcript_path") or "")
            if path.exists(): speakers = json.loads(path.read_text(encoding="utf-8")).get("speakers") or {}
        except Exception: speakers = {}
        speakers = {key: {k: v for k, v in info.items() if k != "centroid"} for key, info in speakers.items()}
        return {"segments": db.transcript(meeting_id), "speakers": speakers}

    @router.patch("/meetings/{meeting_id}/transcript/{segment_id}")
    def edit_segment(meeting_id: str, segment_id: int, body: SegmentEdit):
        if body.text is None and body.speaker is None: raise HTTPException(422, "Nothing to change")
        try: return service.edit_segment(meeting_id, segment_id, text=body.text, speaker=body.speaker)
        except KeyError as exc: raise HTTPException(404, str(exc))

    @router.post("/meetings/{meeting_id}/speakers")
    def name_speaker(meeting_id: str, body: SpeakerNameRequest):
        try:
            return service.name_speaker(meeting_id, body.name, speaker_id=body.speaker_id,
                                        current_label=body.current_label, remember=body.remember)
        except KeyError as exc: raise HTTPException(404, str(exc))
        except ValueError as exc: raise HTTPException(409, str(exc))

    @router.get("/meetings/{meeting_id}/audio")
    def meeting_audio(meeting_id: str, track: str = Query("mix")):
        """Stream a saved recording so evidence can be played back. Supports range requests."""
        value = db.get_meeting(meeting_id)
        if not value or not value.get("audio_path"): raise HTTPException(404, "Meeting not found")
        audio = Path(value["audio_path"])
        candidates = {"mix": audio, "system": audio.parent / "recording-system.wav", "microphone": audio.parent / "recording-microphone.wav"}
        path = candidates.get(track)
        if path is None: raise HTTPException(422, "track must be mix, system, or microphone")
        if not path.exists() or path.stat().st_size == 0:
            path = next((p for p in candidates.values() if p.exists() and p.stat().st_size > 0), None)
            if path is None: raise HTTPException(404, "No recording is saved for this meeting")
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store", "Accept-Ranges": "bytes"})

    @router.get("/voices")
    def voices(): return [{"name": p["name"], "samples": p["samples"], "updated_at": p["updated_at"]} for p in db.voice_profiles()]

    @router.delete("/voices/{name}")
    def delete_voice(name: str):
        if not db.delete_voice_profile(name): raise HTTPException(404, "Voice profile not found")
        return {"deleted": name}

    @router.get("/templates")
    def templates(): return list_templates()

    @router.get("/meetings/{meeting_id}/analysis")
    def analysis(meeting_id: str):
        value = db.get_meeting(meeting_id)
        if not value: raise HTTPException(404, "Meeting not found")
        path = value.get("analysis_path")
        if not path or not Path(path).exists(): raise HTTPException(404, "Analysis is not available")
        result = json.loads(Path(path).read_text())
        result.setdefault("meeting", {})["title"] = value["title"]
        result["people_mentioned"] = [{
            "name": person["name"], "context": person.get("context") or "",
            "importance": person.get("importance") or "medium",
        } for person in value["people"] if person.get("type") == "mentioned"]
        return result

    @router.get("/meetings/{meeting_id}/report.pdf")
    def meeting_report(meeting_id: str):
        value = db.get_meeting(meeting_id)
        if not value:
            raise HTTPException(404, "Meeting not found")
        if value.get("status") != "completed":
            raise HTTPException(409, "The PDF report is available after meeting analysis is completed")
        try:
            document = build_meeting_report(value, settings.user_name, settings.aliases)
        except Exception:
            log.exception("PDF report generation failed meeting_id=%s", meeting_id)
            raise HTTPException(500, "PDF report could not be generated. Your saved meeting remains unchanged.")
        filename = report_filename(value.get("title") or "Meeting", value.get("started_at") or "")
        return Response(
            content=document,
            media_type="application/pdf",
            headers={
                "Content-Disposition": content_disposition(filename),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.patch("/meetings/{meeting_id}/info")
    def update_meeting_info(meeting_id: str, body: MeetingInfoUpdate):
        try:
            return db.update_meeting_info(
                meeting_id, body.title, [person.model_dump() for person in body.people_mentioned]
            )
        except KeyError:
            raise HTTPException(404, "Meeting not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @router.delete("/meetings/{meeting_id}")
    def delete_meeting(meeting_id: str):
        try:
            return service.delete_meeting(meeting_id)
        except KeyError:
            raise HTTPException(404, "Meeting not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        except OSError as exc:
            log.exception("meeting files could not be removed meeting_id=%s", meeting_id)
            raise HTTPException(500, f"The meeting files could not be removed: {exc}")

    @router.get("/tasks")
    def tasks(): return db.tasks()

    @router.get("/tasks/me")
    def my_tasks():
        all_tasks = []
        seen = set()
        for alias in settings.aliases:
            for task in db.tasks(alias):
                if task["id"] not in seen: all_tasks.append(task); seen.add(task["id"])
        return all_tasks

    @router.patch("/tasks/{task_id}")
    def update_task(task_id: str, body: TaskUpdate):
        if body.status not in ("open", "completed"): raise HTTPException(422, "Status must be open or completed")
        if not db.update_task(task_id, body.status): raise HTTPException(404, "Task not found")
        return {"id": task_id, "status": body.status}

    @router.post("/recording/start")
    def start(body: StartRequest):
        try: return service.start(body.title, body.meet_url, template=body.template, platform=body.platform)
        except Exception as exc: raise HTTPException(409, str(exc))

    @router.post("/recording/stop")
    async def stop():
        try: return await service.stop_and_process()
        except Exception as exc: raise HTTPException(409, str(exc))

    @router.post("/meetings/{meeting_id}/retry")
    async def retry(meeting_id: str, body: RetryRequest = RetryRequest()):
        try:
            source = await service.retry(meeting_id, retranscribe=body.retranscribe)
            return {"status": "processing", "meeting_id": meeting_id, "source": source}
        except Exception as exc: raise HTTPException(409, str(exc))

    @router.get("/audio/devices")
    def audio_devices(): return AudioDeviceManager().report()

    @router.get("/settings")
    def app_settings():
        return {"profile": {"name": settings.user_name, "aliases": settings.aliases},
            "ai": {"whisper_model": settings.whisper_model, "whisper_language": settings.whisper_language or "Automatic",
                   "whisper_task": settings.whisper_task, "ollama_model": settings.ollama_model,
                   "ollama_num_ctx": settings.ollama_num_ctx, "ollama": service.llm.availability(),
                   "whisper": service.transcriber.availability()}, "audio": AudioDeviceManager().report(),
            "storage": str(settings.recordings_path), "privacy": "All processing and storage remain local."}

    return router
