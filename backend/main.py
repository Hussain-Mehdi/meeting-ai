import asyncio
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.api.routes import create_router
from backend.config import get_settings
from backend.database.db import Database
from backend.detection.meet_detector import MeetDetector
from backend.meetings.service import MeetingService
from backend.notifications.macos import notify
from backend.state import MeetingState, runtime


settings = get_settings()
handler = RotatingFileHandler("logs/app.log", maxBytes=2_000_000, backupCount=3)
logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()], format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)
db = Database(settings.database_path)
recovered_meetings = db.recover_interrupted_meetings()
if recovered_meetings:
    log.warning("marked interrupted meetings as retryable count=%s", recovered_meetings)
service = MeetingService(settings, db)
detector = MeetDetector()


async def detected(tab):
    status = runtime.snapshot()
    if status["state"] in ("idle", "completed", "failed"):
        if status["state"] != "idle": runtime.transition(MeetingState.IDLE)
        runtime.transition(MeetingState.DETECTED, meeting_detected=True, detected_title=tab.title, detected_url=tab.url)
        runtime.transition(MeetingState.WAITING_FOR_CONFIRMATION)
        notify("Google Meet detected", "A meeting appears to be open. Open Meeting AI to start recording.", True)
        log.info("Meet detected url=%s", tab.url)


async def ended(tab):
    status = runtime.snapshot()
    if status["recording"]:
        try: await service.stop_and_process()
        except Exception: log.exception("automatic recording stop failed")
    elif status["state"] in ("detected", "waiting_for_confirmation"):
        runtime.transition(MeetingState.IDLE, meeting_detected=False, detected_title=None, detected_url=None)


@asynccontextmanager
async def lifespan(app):
    log.info("Meeting AI startup")
    task = asyncio.create_task(detector.monitor(detected, ended, settings.detection_interval))
    yield
    task.cancel()


app = FastAPI(title="Meeting AI", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"], allow_methods=["*"], allow_headers=["*"])
app.include_router(create_router(db, service))
