"""Application settings, loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "SBB Disruption Companion"
    database_url: str = "postgresql+asyncpg://sbb:sbb@localhost:5432/sbb"

    # Google Gemini (optional: the app falls back to deterministic templates without it)
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"
    # Fallback model(s), comma separated, tried in order when the main model is overloaded (503) / slow / unavailable.
    gemini_fallback_model: str = "gemini-3.5-flash"
    gemini_timeout_seconds: float = 50.0  # TOTAL wait per answer across retries and models, then the rule engine answers
    gemini_attempt_timeout_seconds: float = 25.0  # one HTTP call (Google rejects deadlines under 10 s, so values below 10 are raised to 10)
    gemini_retries: int = 1  # extra tries per model for 503 / 429 / 5xx (not for timeouts: the next model gets the turn)
    gemini_retry_backoff_seconds: float = 1.0
    # Gemini 3+ models think before answering. MINIMAL is fastest; LOW | MEDIUM | HIGH think longer.
    gemini_thinking_level: str = "minimal"
    gemini_max_output_tokens: int = 900
    # schema = Pydantic response_schema | jsonschema = raw JSON schema | json = JSON mime type only (fastest on some models)
    gemini_output_mode: str = "schema"
    # async = answer instantly from the SBB rules and refine with Gemini in the background; sync = wait for Gemini
    gemini_mode: str = "async"
    gemini_cooldown_seconds: float = 60.0  # circuit breaker after quota / repeated errors

    # Anonymous chat sessions
    session_ttl_hours: int = 24
    session_cleanup_interval_seconds: int = 600

    # Data / ETL
    data_dir: Path = Field(default=PROJECT_ROOT / "Daten")
    stations_file: Path = Field(default=PROJECT_ROOT / "data" / "stations.csv")
    auto_ingest: bool = True  # run the ETL on startup when the DB has no incidents
    dataset_from: str = "2026-09-01"
    dataset_to: str = "2026-10-13"
    live_incident_count: int = 6

    log_level: str = "INFO"
    log_json: bool = True
    cors_origins: str = "*"


@lru_cache
def get_settings() -> Settings:
    return Settings()
