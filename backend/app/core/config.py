"""Application configuration — 12-factor, typed, sourced from the environment."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONYX_", env_file=".env", extra="ignore")

    # --- app ---
    environment: str = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # --- database (async URL for the app; sync URL for Alembic) ---
    database_url: str = "postgresql+asyncpg://onyx_app_rw@localhost:5432/onyx"
    database_url_sync: str = "postgresql+psycopg2://onyx_migrator@localhost:5432/onyx"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # --- redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- auth / JWT ---
    jwt_secret: str = Field(default="dev-insecure-change-me")
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 30 * 24 * 3600

    # --- rate limiting ---
    rate_limit_auth_per_minute: int = 10
    rate_limit_analysis_per_minute: int = 30

    # --- object storage ---
    s3_endpoint_url: str | None = None
    s3_bucket_documents: str = "onyx-documents"

    # --- ai ---
    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-4-8"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
