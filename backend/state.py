from dataclasses import asdict, dataclass
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
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"


ALLOWED = {
    MeetingState.IDLE: {MeetingState.DETECTED, MeetingState.RECORDING, MeetingState.TRANSCRIBING},
    MeetingState.DETECTED: {MeetingState.WAITING_FOR_CONFIRMATION, MeetingState.IDLE},
    MeetingState.WAITING_FOR_CONFIRMATION: {MeetingState.RECORDING, MeetingState.IDLE},
    MeetingState.RECORDING: {MeetingState.RECORDED, MeetingState.FAILED},
    MeetingState.RECORDED: {MeetingState.TRANSCRIBING, MeetingState.FAILED},
    MeetingState.TRANSCRIBING: {MeetingState.ANALYZING, MeetingState.FAILED},
    MeetingState.ANALYZING: {MeetingState.COMPLETED, MeetingState.FAILED},
    MeetingState.COMPLETED: {MeetingState.IDLE, MeetingState.RECORDING, MeetingState.TRANSCRIBING},
    MeetingState.FAILED: {MeetingState.TRANSCRIBING, MeetingState.IDLE, MeetingState.RECORDING},
}


@dataclass
class RuntimeStatus:
    state: MeetingState = MeetingState.IDLE
    meeting_id: Optional[str] = None
    meeting_detected: bool = False
    recording: bool = False
    stage: str = "ready"
    progress: int = 0
    stage_detail: str = "Ready"
    processing_started_at: Optional[str] = None
    error: Optional[str] = None
    detected_title: Optional[str] = None
    detected_url: Optional[str] = None


class StateMachine:
    def __init__(self) -> None:
        self._value = RuntimeStatus()
        self._lock = RLock()

    def transition(self, target: MeetingState, **updates) -> None:
        with self._lock:
            if target != self._value.state and target not in ALLOWED[self._value.state]:
                raise ValueError(f"Invalid transition: {self._value.state} -> {target}")
            self._value.state = target
            self._value.stage = target.value
            for key, value in updates.items():
                setattr(self._value, key, value)

    def update(self, **updates) -> None:
        with self._lock:
            for key, value in updates.items():
                setattr(self._value, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            result = asdict(self._value)
            result["state"] = self._value.state.value
            return result


runtime = StateMachine()
