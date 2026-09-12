from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    user_name: str = "Hussain"
    user_aliases: str = "Hussain,Husain"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3:14b"
    ollama_num_ctx: int = 32768
    whisper_model: str = "large-v3"
    whisper_language: str = ""
    # "transcribe" keeps the original spoken language as the source of truth; an English
    # rendering is produced separately (see whisper_translation). "translate" is the legacy
    # behaviour that translates while transcribing and loses the original wording.
    whisper_task: str = "transcribe"
    # auto: English pass only when the detected language is not English; always; never.
    whisper_translation: str = "auto"
    # auto: mlx-whisper on Metal when available (Apple Silicon), else faster-whisper on CPU.
    whisper_backend: str = "auto"
    whisper_mlx_repo: str = ""
    # Once weights are cached, never contact the network for them again.
    whisper_offline: bool = True
    # Speaker diarization of the system-audio track (remote participants).
    diarization_enabled: bool = True
    diarization_threshold: float = 0.62
    voice_match_threshold: float = 0.70
    whisper_initial_prompt: str = "Software engineering meeting. Preserve names and technical terms."
    database_path: Path = Path("data/meetings.db")
    recordings_path: Path = Path("data/meetings")
    detection_interval: float = 3.0
    detection_timeout: float = 15.0
    # Consecutive "no Meet tab" readings required before a recording is auto-stopped.
    detection_end_confirmations: int = 5
    # Chromium-family browsers to scan for Meet / Zoom web / Teams web tabs (comma separated).
    detection_browsers: str = "Google Chrome"
    detect_zoom_app: bool = True
    detect_teams_app: bool = True
    max_recording_hours: float = 4.0
    resume_interrupted_processing: bool = True
    # Whisper/Ollama never start while a recording is being captured, so the
    # capture (the source of truth for the transcript) always gets full priority.
    defer_processing_while_recording: bool = True
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def browsers(self) -> list[str]:
        return [x.strip() for x in self.detection_browsers.split(",") if x.strip()]

    @property
    def aliases(self) -> list[str]:
        return [x.strip() for x in self.user_aliases.split(",") if x.strip()]

    def ensure_dirs(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.recordings_path.mkdir(parents=True, exist_ok=True)
        Path("logs").mkdir(exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    value = Settings()
    value.ensure_dirs()
    return value
