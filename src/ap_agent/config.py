"""Runtime configuration, read from environment variables and an optional .env file.

Provider and model names live here only, so orchestration code never hard-codes them.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Local Supabase Postgres (`supabase start`); well-known local-only default.
    database_url: SecretStr = SecretStr("postgresql://postgres:postgres@127.0.0.1:54322/postgres")

    llm_provider: str = "gemini"
    llm_model: str = "gemini-2.5-flash"
    llm_api_key: SecretStr | None = None

    embed_provider: str = "gemini"
    embed_model: str = "gemini-embedding-001"
    embed_dim: int = Field(default=768, gt=0)

    corpus_source: Path = Path("data/corpus")

    # Budgets and tool reliability (design §6, §12).
    max_steps: int = Field(default=12, gt=0)
    max_tool_calls: int = Field(default=8, gt=0)
    tool_timeout_s: float = Field(default=3.0, gt=0)
    tool_max_retries: int = Field(default=2, ge=0)

    retrieval_min_score: float = Field(default=0.5, ge=0, le=1)

    # Fault injection, e.g. "get_purchase_order:timeout".
    faults: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
