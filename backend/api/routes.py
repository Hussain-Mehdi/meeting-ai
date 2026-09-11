import json
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Response
from typing import Literal
from pydantic import BaseModel, Field, field_validator
from backend.audio.devices import AudioDeviceManager
from backend.config import get_settings
from backend.reports.pdf import build_meeting_report, content_disposition, report_filename
from backend.state import runtime


log = logging.getLogger(__name__)


class StartRequest(BaseModel):
    title: str = "Untitled meeting"
    meet_url: str | None = None


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

    @router.get("/meetings/{meeting_id}")
    def meeting(meeting_id: str):
        value = db.get_meeting(meeting_id)
        if not value: raise HTTPException(404, "Meeting not found")
        return add_user_ownership(value)

    @router.get("/meetings/{meeting_id}/transcript")
    def transcript(meeting_id: str): return db.transcript(meeting_id)

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
        try: return service.start(body.title, body.meet_url)
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
