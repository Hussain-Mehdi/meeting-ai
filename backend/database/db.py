import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from backend.analysis.schemas import MeetingAnalysis


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meetings (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, meet_url TEXT, started_at TEXT NOT NULL,
 ended_at TEXT, duration_seconds INTEGER DEFAULT 0, audio_path TEXT, transcript_path TEXT,
 analysis_path TEXT, summary TEXT DEFAULT '', status TEXT NOT NULL, error TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript_segments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT NOT NULL, start REAL, end REAL,
 speaker TEXT, text TEXT NOT NULL, FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS people (
 id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, name TEXT, type TEXT, context TEXT,
 importance TEXT, FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, owner TEXT, task TEXT,
 deadline_original TEXT, deadline_normalized TEXT, priority TEXT, confidence TEXT,
 evidence TEXT, status TEXT DEFAULT 'open', created_at TEXT NOT NULL,
 FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS requested_changes (
 id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, requested_of TEXT NOT NULL,
 change_text TEXT NOT NULL, deadline_original TEXT, deadline_normalized TEXT,
 priority TEXT, confidence TEXT, evidence TEXT,
 FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, meeting_id TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS goals (id TEXT PRIMARY KEY, meeting_id TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS key_topics (id TEXT PRIMARY KEY, meeting_id TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS next_steps (id TEXT PRIMARY KEY, meeting_id TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS meeting_info_edits (
 meeting_id TEXT PRIMARY KEY, title TEXT NOT NULL, people_json TEXT NOT NULL,
 updated_at TEXT NOT NULL, FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def create_meeting(self, meeting_id, title, started_at, audio_path, meet_url=None):
        with self.connection() as db:
            db.execute("INSERT INTO meetings(id,title,meet_url,started_at,audio_path,status,created_at) VALUES(?,?,?,?,?,'recording',?)",
                       (meeting_id, title, meet_url, started_at, str(audio_path), datetime.now(timezone.utc).isoformat()))

    def finish_recording(self, meeting_id, ended_at, duration_seconds):
        with self.connection() as db:
            db.execute("UPDATE meetings SET ended_at=?,duration_seconds=?,status='recorded' WHERE id=?",
                       (ended_at, duration_seconds, meeting_id))

    def set_status(self, meeting_id, status, error=None):
        with self.connection() as db:
            db.execute("UPDATE meetings SET status=?,error=? WHERE id=?", (status, error, meeting_id))

    def recover_interrupted_meetings(self) -> int:
        """Convert stale in-progress rows into explicit, retryable failures after an app restart."""
        message = ("Meeting AI stopped before processing finished. Any recording or transcript already "
                   "saved was not deleted. Open this meeting to retry from the latest safe checkpoint.")
        with self.connection() as db:
            changed = db.execute("""UPDATE meetings
                SET status='failed', error=?, ended_at=COALESCE(ended_at, started_at),
                    duration_seconds=CASE WHEN duration_seconds < 1 THEN 1 ELSE duration_seconds END
                WHERE status IN ('recording','recorded','transcribing','analyzing')""", (message,)).rowcount
        return changed

    def save_transcript(self, meeting_id, segments, transcript_path):
        with self.connection() as db:
            db.execute("DELETE FROM transcript_segments WHERE meeting_id=?", (meeting_id,))
            db.executemany("INSERT INTO transcript_segments(meeting_id,start,end,speaker,text) VALUES(?,?,?,?,?)",
                           [(meeting_id, s["start"], s["end"], s.get("speaker", "Speaker"), s["text"]) for s in segments])
            db.execute("UPDATE meetings SET transcript_path=? WHERE id=?", (str(transcript_path), meeting_id))

    def save_analysis(self, meeting_id: str, analysis: MeetingAnalysis, path: Path):
        with self.connection() as db:
            manual = db.execute("SELECT title,people_json FROM meeting_info_edits WHERE meeting_id=?", (meeting_id,)).fetchone()
            title = manual["title"] if manual else analysis.meeting.title
            db.execute("UPDATE meetings SET title=?,summary=?,analysis_path=?,status='completed',error=NULL WHERE id=?",
                       (title, analysis.summary, str(path), meeting_id))
            for table in ("people", "tasks", "requested_changes", "decisions", "goals", "key_topics", "next_steps"):
                db.execute(f"DELETE FROM {table} WHERE meeting_id=?", (meeting_id,))
            for p in analysis.people_mentioned:
                db.execute("INSERT INTO people VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, meeting_id, p.name, "mentioned", p.context, p.importance))
            for p in analysis.attendees:
                db.execute("INSERT INTO people VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, meeting_id, p.name, "attendee", "", p.confidence))
            if manual:
                db.execute("DELETE FROM people WHERE meeting_id=? AND type='mentioned'", (meeting_id,))
                for p in json.loads(manual["people_json"]):
                    db.execute("INSERT INTO people VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, meeting_id, p["name"], "mentioned", p.get("context", ""), p.get("importance", "medium")))
            for task in analysis.tasks:
                deadline = task.deadline
                db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,'open',?)", (uuid.uuid4().hex, meeting_id, task.owner, task.task,
                    deadline.original if deadline else None, deadline.normalized if deadline else None,
                    task.priority, task.confidence, task.evidence, datetime.now(timezone.utc).isoformat()))
            for change in analysis.requested_changes:
                deadline = change.deadline
                db.execute("INSERT INTO requested_changes VALUES(?,?,?,?,?,?,?,?,?)", (
                    uuid.uuid4().hex, meeting_id, change.requested_of, change.change,
                    deadline.original if deadline else None, deadline.normalized if deadline else None,
                    change.priority, change.confidence, change.evidence,
                ))
            for table, values in (("decisions", analysis.decisions), ("goals", analysis.goals), ("key_topics", analysis.key_topics),
                                  ("next_steps", analysis.next_steps)):
                db.executemany(f"INSERT INTO {table} VALUES(?,?,?)", [(uuid.uuid4().hex, meeting_id, x) for x in values])

    def clear_analysis(self, meeting_id: str):
        """Remove derived claims while preserving the meeting, recording, and transcript."""
        with self.connection() as db:
            for table in ("people", "tasks", "requested_changes", "decisions", "goals", "key_topics", "next_steps"):
                db.execute(f"DELETE FROM {table} WHERE meeting_id=?", (meeting_id,))
            db.execute("UPDATE meetings SET summary='', analysis_path=NULL WHERE id=?", (meeting_id,))

    def list_meetings(self, query=None):
        with self.connection() as db:
            if query:
                term = f"%{query}%"
                rows = db.execute("""SELECT DISTINCT m.* FROM meetings m LEFT JOIN transcript_segments s ON s.meeting_id=m.id
                  LEFT JOIN tasks t ON t.meeting_id=m.id LEFT JOIN people p ON p.meeting_id=m.id
                  LEFT JOIN requested_changes r ON r.meeting_id=m.id
                  WHERE m.title LIKE ? OR m.summary LIKE ? OR s.text LIKE ? OR t.task LIKE ? OR p.name LIKE ?
                  OR r.change_text LIKE ? ORDER BY m.started_at DESC""", (term,)*6).fetchall()
            else:
                rows = db.execute("SELECT * FROM meetings ORDER BY started_at DESC").fetchall()
            return [dict(x) for x in rows]

    def get_meeting(self, meeting_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if not row: return None
            result = dict(row)
            for table in ("people", "tasks", "requested_changes", "decisions", "goals", "key_topics", "next_steps"):
                result[table] = [dict(x) for x in db.execute(f"SELECT * FROM {table} WHERE meeting_id=?", (meeting_id,))]
            audio = Path(result["audio_path"]) if result.get("audio_path") else None
            candidates = [audio, audio.parent / "recording-system.wav", audio.parent / "recording-microphone.wav"] if audio else []
            result["recording_available"] = any(path.exists() and path.stat().st_size > 0 for path in candidates)
            transcript_on_disk = bool(result.get("transcript_path") and Path(result["transcript_path"]).exists())
            transcript_in_db = db.execute("SELECT 1 FROM transcript_segments WHERE meeting_id=? LIMIT 1", (meeting_id,)).fetchone() is not None
            result["transcript_available"] = transcript_on_disk or transcript_in_db
            result["can_retry"] = result["recording_available"] or result["transcript_available"]
            return result

    def transcript(self, meeting_id):
        with self.connection() as db:
            return [dict(x) for x in db.execute("SELECT start,end,speaker,text FROM transcript_segments WHERE meeting_id=? ORDER BY start", (meeting_id,))]

    def tasks(self, owner=None):
        with self.connection() as db:
            sql = "SELECT t.*,m.title meeting_title FROM tasks t JOIN meetings m ON m.id=t.meeting_id"
            args = ()
            if owner:
                sql += " WHERE lower(t.owner)=lower(?)"; args = (owner,)
            sql += " ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.deadline_normalized"
            return [dict(x) for x in db.execute(sql, args)]

    def update_task(self, task_id, status):
        with self.connection() as db:
            changed = db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id)).rowcount
        return bool(changed)

    def update_meeting_info(self, meeting_id: str, title: str, people_mentioned: list[dict]):
        """Update user-controlled metadata without changing any generated analysis content."""
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Meeting title cannot be empty")

        clean_people = []
        seen = set()
        for person in people_mentioned:
            name = str(person.get("name", "")).strip()
            if not name:
                raise ValueError("A meeting member name cannot be empty")
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            clean_people.append({
                "name": name,
                "context": str(person.get("context", "")).strip(),
                "importance": person.get("importance", "medium"),
            })

        with self.connection() as db:
            meeting = db.execute("SELECT status FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if not meeting:
                raise KeyError("Meeting not found")
            if meeting["status"] != "completed":
                raise ValueError("Meeting information can only be edited after analysis is completed")

            now = datetime.now(timezone.utc).isoformat()
            db.execute("UPDATE meetings SET title=? WHERE id=?", (clean_title, meeting_id))
            db.execute("DELETE FROM people WHERE meeting_id=? AND type='mentioned'", (meeting_id,))
            for person in clean_people:
                db.execute("INSERT INTO people VALUES(?,?,?,?,?,?)", (
                    uuid.uuid4().hex, meeting_id, person["name"], "mentioned",
                    person["context"], person["importance"],
                ))
            db.execute("""INSERT INTO meeting_info_edits(meeting_id,title,people_json,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(meeting_id) DO UPDATE SET
                title=excluded.title,people_json=excluded.people_json,updated_at=excluded.updated_at""",
                (meeting_id, clean_title, json.dumps(clean_people), now))
        return self.get_meeting(meeting_id)
