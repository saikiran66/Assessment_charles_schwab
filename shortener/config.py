"""Application configuration."""

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    host: str = "127.0.0.1"
    port: int = 8000
    base_url: str = "http://127.0.0.1:8000"
    max_url_length: int = 2048
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=Path(os.environ.get("SHORTENER_DB", "data/shortener.sqlite3")),
            host=os.environ.get("SHORTENER_HOST", "127.0.0.1"),
            port=int(os.environ.get("SHORTENER_PORT", "8000")),
            base_url=os.environ.get("SHORTENER_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            max_url_length=int(os.environ.get("SHORTENER_MAX_URL_LENGTH", "2048")),
            max_retries=int(os.environ.get("ORCHESTRATOR_MAX_RETRIES", "2")),
        )
