import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from backend.audio.devices import AudioDeviceManager
from backend.config import get_settings
from backend.state import runtime


class StartRequest(BaseModel):
    title: str = "Untitled meeting"
    meet_url: str | None = None


class TaskUpdate(BaseModel):
    status: str


class RetryRequest(BaseModel):
    retranscribe: bool = False


def create_router(db, service):
    router = APIRouter(prefix="/api")
    settings = get_settings()

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
        return value

    @router.get("/meetings/{meeting_id}/transcript")
    def transcript(meeting_id: str): return db.transcript(meeting_id)

    @router.get("/meetings/{meeting_id}/analysis")
    def analysis(meeting_id: str):
        value = db.get_meeting(meeting_id)
        if not value: raise HTTPException(404, "Meeting not found")
        path = value.get("analysis_path")
        if not path or not Path(path).exists(): raise HTTPException(404, "Analysis is not available")
        return json.loads(Path(path).read_text())

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
                   "whisper_task": settings.whisper_task, "ollama_model": settings.ollama_model, "ollama": service.llm.availability(),
                   "whisper": service.transcriber.availability()}, "audio": AudioDeviceManager().report(),
            "storage": str(settings.recordings_path), "privacy": "All processing and storage remain local."}

    return router
