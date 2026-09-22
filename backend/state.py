from dataclasses import asdict, dataclass, field
from enum import Enum
from threading import RLock
from typing import Optional


class MeetingState(str, Enum):
    IDLE = "idle"
    DETECTED = "detected"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    RECORDING = "recording"
    RECORDED = "recorded"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"


# Recording and processing are independent: a new meeting can be recorded while the
# previous one is still being transcribed or analyzed in the background.
RECORDING_ALLOWED = {
    MeetingState.IDLE: {MeetingState.DETECTED, MeetingState.RECORDING},
    MeetingState.DETECTED: {MeetingState.WAITING_FOR_CONFIRMATION, MeetingState.IDLE},
    MeetingState.WAITING_FOR_CONFIRMATION: {MeetingState.RECORDING, MeetingState.IDLE},
    MeetingState.RECORDING: {MeetingState.IDLE, MeetingState.FAILED},
    MeetingState.FAILED: {MeetingState.IDLE, MeetingState.DETECTED, MeetingState.RECORDING},
}

PROCESSING_ALLOWED = {
    MeetingState.IDLE: {MeetingState.RECORDED, MeetingState.TRANSCRIBING},
    MeetingState.RECORDED: {MeetingState.TRANSCRIBING, MeetingState.FAILED},
    MeetingState.TRANSCRIBING: {MeetingState.TRANSCRIBED, MeetingState.ANALYZING, MeetingState.FAILED},
    # Transcript is waiting for the user's review; the worker is free for the next meeting.
    MeetingState.TRANSCRIBED: {MeetingState.IDLE, MeetingState.RECORDED, MeetingState.TRANSCRIBING},
    MeetingState.ANALYZING: {MeetingState.COMPLETED, MeetingState.FAILED},
    MeetingState.COMPLETED: {MeetingState.IDLE, MeetingState.RECORDED, MeetingState.TRANSCRIBING},
    MeetingState.FAILED: {MeetingState.IDLE, MeetingState.RECORDED, MeetingState.TRANSCRIBING},
}

PROCESSING_BUSY = {MeetingState.RECORDED, MeetingState.TRANSCRIBING, MeetingState.ANALYZING}


@dataclass
class ProcessingStatus:
    state: MeetingState = MeetingState.IDLE
    meeting_id: Optional[str] = None
    progress: int = 0
    stage_detail: str = "Nothing to process"
    processing_started_at: Optional[str] = None
    error: Optional[str] = None
    queued: list = field(default_factory=list)


@dataclass
class RuntimeStatus:
    state: MeetingState = MeetingState.IDLE
    meeting_id: Optional[str] = None
    meeting_detected: bool = False
    recording: bool = False
    stage: str = "ready"
    stage_detail: str = "Ready"
    error: Optional[str] = None
    detected_title: Optional[str] = None
    detected_url: Optional[str] = None
    detected_platform: Optional[str] = None
    processing: ProcessingStatus = field(default_factory=ProcessingStatus)


class StateMachine:
    def __init__(self) -> None:
        self._value = RuntimeStatus()
        self._lock = RLock()

    @staticmethod
    def _apply(target_obj, allowed, target: MeetingState, updates: dict) -> None:
        if target != target_obj.state and target not in allowed[target_obj.state]:
            raise ValueError(f"Invalid transition: {target_obj.state} -> {target}")
        target_obj.state = target
        for key, value in updates.items():
            setattr(target_obj, key, value)

    def transition(self, target: MeetingState, **updates) -> None:
        """Move the recording lifecycle (idle → detected → waiting → recording → idle)."""
        with self._lock:
            self._apply(self._value, RECORDING_ALLOWED, target, updates)
            self._value.stage = target.value

    def update(self, **updates) -> None:
        with self._lock:
            for key, value in updates.items():
                setattr(self._value, key, value)

    def processing_transition(self, target: MeetingState, **updates) -> None:
        """Move the background processing pipeline (recorded → transcribing → analyzing → completed/failed)."""
        with self._lock:
            self._apply(self._value.processing, PROCESSING_ALLOWED, target, updates)

    def processing_update(self, **updates) -> None:
        with self._lock:
            for key, value in updates.items():
                setattr(self._value.processing, key, value)

    def processing_busy(self) -> bool:
        with self._lock:
            return self._value.processing.state in PROCESSING_BUSY

    def snapshot(self) -> dict:
        with self._lock:
            result = asdict(self._value)
            result["state"] = self._value.state.value
            result["processing"]["state"] = self._value.processing.state.value
            result["processing"]["queued"] = list(self._value.processing.queued)
            return result


runtime = StateMachine()
