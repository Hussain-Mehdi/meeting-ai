from typing import Literal, Optional
from pydantic import BaseModel, Field


Confidence = Literal["high", "medium", "low"]
Priority = Literal["high", "medium", "low"]


class MeetingMetadata(BaseModel):
    title: str
    date: str
    start_time: str
    end_time: str
    duration_minutes: int


class Attendee(BaseModel):
    name: str
    confidence: Confidence


class PersonMention(BaseModel):
    name: str
    context: str = Field(min_length=1)
    importance: Priority = "medium"


class Deadline(BaseModel):
    original: str
    normalized: Optional[str] = None


class Task(BaseModel):
    owner: str
    task: str
    deadline: Optional[Deadline] = None
    priority: Priority = "medium"
    confidence: Confidence
    evidence: str = Field(min_length=1)


class RequestedChange(BaseModel):
    requested_of: str
    change: str
    deadline: Optional[Deadline] = None
    priority: Priority = "medium"
    confidence: Confidence
    evidence: str = Field(min_length=1)


class MeetingAnalysis(BaseModel):
    meeting: MeetingMetadata
    summary: str
    goals: list[str] = Field(default_factory=list)
    key_topics: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    attendees: list[Attendee] = Field(default_factory=list)
    people_mentioned: list[PersonMention] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    requested_changes: list[RequestedChange] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
