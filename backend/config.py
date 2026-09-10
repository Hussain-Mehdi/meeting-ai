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
    whisper_task: str = "translate"
    whisper_initial_prompt: str = "Software engineering meeting. Preserve names and technical terms."
    database_path: Path = Path("data/meetings.db")
    recordings_path: Path = Path("data/meetings")
    detection_interval: float = 3.0
    max_recording_hours: float = 4.0
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
