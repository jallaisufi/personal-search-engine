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
    # Connections per process and database. Total = processes x (size + overflow);
    # keep it below Postgres max_connections (100 by default, 200 in scaled mode).
    db_pool_size: int = 5
    db_pool_overflow: int = 5

    local_docs_dir: str = "./sample-docs"
    sync_interval_seconds: int = 60

    # Google Drive (optional). Each self-hoster creates their own OAuth client.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:8080/api/sources/gdrive/callback"
    encryption_key: str | None = None  # encrypts stored OAuth tokens
    drive_max_file_mb: int = 50  # larger files are skipped

    # Ingestion queue
    ingest_batch_size: int = 8  # jobs a worker takes per read
    ingest_max_attempts: int = 3  # then the document is marked failed (dead letter)
    ingest_retry_base_seconds: float = 30  # backoff: 30 s, 2 min, 8 min, ...

    embedding_provider: Literal["ollama", "openai", "fake"] = "ollama"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768
    embedding_batch_size: int = 32
    ollama_url: str = "http://localhost:11434"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    # Ask mode: the model that writes answers from retrieved passages.
    llm_provider: Literal["ollama", "openai", "anthropic", "fake"] = "ollama"
    llm_model: str = "llama3.2:3b"
    ask_sources: int = 6  # passages given to the model per question

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
        if self.llm_provider == "openai" and not self.openai_api_key:
            raise ValueError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            raise ValueError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        if self.google_client_id and not (self.google_client_secret and self.encryption_key):
            raise ValueError(
                "Google Drive needs GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and ENCRYPTION_KEY"
            )
        if self.chunk_overlap_tokens >= self.chunk_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be smaller than CHUNK_TOKENS")
        return self

    @property
    def read_url(self) -> str:
        return self.database_read_url or self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
