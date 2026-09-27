"""Runtime configuration, read from environment variables and an optional .env file.

Provider and model names live here only, so orchestration code never hard-codes them.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Local Postgres from compose.yaml; local-only development credentials.
    database_url: SecretStr = SecretStr("postgresql://ap_app:ap_app@127.0.0.1:5433/ap_agent")

    llm_provider: str = "gemini"
    llm_model: str = "gemini-2.5-flash"
    llm_api_key: SecretStr | None = None

    embed_provider: Literal["gemini", "fake"] = "gemini"  # fake: offline, for tests
    embed_model: str = "gemini-embedding-001"
    embed_dim: int = Field(default=768, gt=0)

    corpus_source: Path = Path("data/corpus")
    golden_queries: Path = Path("data/golden_queries.yaml")

    # Budgets and tool reliability (design §6, §12).
    max_steps: int = Field(default=12, gt=0)
    max_tool_calls: int = Field(default=8, gt=0)
    tool_timeout_s: float = Field(default=3.0, gt=0)
    tool_max_retries: int = Field(default=2, ge=0)

    # Calibrated on gemini-embedding-001 at 768-d: relevant policy scores 0.62 and up,
    # off-topic questions at most 0.58.
    retrieval_min_score: float = Field(default=0.6, ge=0, le=1)

    # Fault injection: comma-separated tool:kind, e.g. "get_purchase_order:timeout".
    # timeout = every attempt hangs past the deadline; transient = the first attempt fails.
    faults: Annotated[dict[str, Literal["timeout", "transient"]], NoDecode] = {}

    @field_validator("faults", mode="before")
    @classmethod
    def _parse_faults(cls, value: object) -> object:
        if isinstance(value, str):
            return dict(item.strip().split(":", 1) for item in value.split(",") if item.strip())
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
