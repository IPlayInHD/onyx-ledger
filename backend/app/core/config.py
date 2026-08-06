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
    # Multi-year projections are governed by published rule metadata; the flag
    # lets the surface be turned off entirely without that looking like "no
    # rule authorized one".
    ioe_projections_enabled: bool = True

    # --- scheduled replay-integrity verification (closure entry 8C) ---
    # Verification REPLAYS a sealed calculation, so a batch costs real engine
    # runs. Every knob below exists so an operator can slow it down or stop it
    # during an incident without a deploy.
    #   ONYX_IOE_INTEGRITY_VERIFICATION_ENABLED  master switch; false makes the
    #                                            scheduled task a no-op
    #   ONYX_IOE_INTEGRITY_BATCH_SIZE            records per execution across
    #                                            ALL target types; hard-capped
    #                                            at 50 by the scheduler and
    #                                            again by the SQL
    #   ONYX_IOE_INTEGRITY_INTERVAL_MINUTES      beat interval
    #   ONYX_IOE_INTEGRITY_TIMEOUT_SECONDS       per-record ceiling, so one
    #                                            pathological record cannot
    #                                            consume the window
    ioe_integrity_verification_enabled: bool = True
    ioe_integrity_batch_size: int = 10
    ioe_integrity_interval_minutes: int = 15
    ioe_integrity_timeout_seconds: float = 60.0

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
    s3_bucket_legislation: str = "onyx-legislation"   # TKMS raw imports + extracted text

    # --- ai ---
    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-4-8"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
