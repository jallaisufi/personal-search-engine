"""All configuration comes from environment variables (.env), validated at startup."""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/search"
    # Read-only traffic (search) goes here. Same as primary in lite mode.
    database_read_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"

    local_docs_dir: str = "./sample-docs"
    sync_interval_seconds: int = 60

    embedding_provider: Literal["ollama", "openai", "fake"] = "ollama"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768
    embedding_batch_size: int = 32
    ollama_url: str = "http://localhost:11434"
    openai_api_key: str | None = None

    chunk_tokens: int = 500
    chunk_overlap_tokens: int = 50

    search_cache_ttl_seconds: int = 600
    search_candidates: int = 50  # top-N from each retriever before fusion
    rrf_k: int = 60  # standard RRF constant

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        # Fail fast at startup instead of on the first request.
        if self.embedding_provider == "openai" and not self.openai_api_key:
            raise ValueError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
        if self.chunk_overlap_tokens >= self.chunk_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be smaller than CHUNK_TOKENS")
        return self

    @property
    def read_url(self) -> str:
        return self.database_read_url or self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
