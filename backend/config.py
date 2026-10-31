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
    # WhatsApp Desktop calls (the call window exists only during a call). WhatsApp Web is not
    # detected: an open tab says nothing about whether a call is running.
    detect_whatsapp_app: bool = True
    # Semantic search over meetings. The embedding model runs in the Ollama already in use;
    # retrieval_mode=bm25 turns all of it off and restores pure keyword search.
    embedding_enabled: bool = True
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 0                     # 0 = discover from the first response
    embedding_batch: int = 16
    embedding_retry_seconds: float = 300.0
    embedding_debounce_seconds: float = 20.0
    retrieval_mode: str = "hybrid"             # hybrid | bm25 | vector
    rrf_k: int = 60
    bm25_weight: float = 1.0
    vector_weight: float = 1.0
    vector_min_score: float = 0.0
    # Web search is the one thing that leaves this machine, so it is opt-in per message and
    # only ever sends the user's own words. This flag decides whether it may be used at all.
    web_search_enabled: bool = True
    web_search_results: int = 5
    web_search_timeout: float = 8.0
    max_recording_hours: float = 4.0
    resume_interrupted_processing: bool = True
    # False: after a recording only the transcript is produced; the user reviews it (edit,
    # delete lines, name speakers) and presses Analyze. True: analysis follows automatically.
    auto_analyze: bool = False
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
