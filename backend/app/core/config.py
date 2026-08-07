"""Application configuration — 12-factor, typed, sourced from the environment."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, ValidationInfo, field_validator, model_validator
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

    # --- admission control (Entry 10) ---
    # These OVERRIDE the compiled defaults in
    # app/services/admission/policy.py, which is where each number is justified.
    # Every one is validated at startup: a negative or zero limit is a
    # configuration error, not a limit that silently never triggers or one that
    # refuses everything.
    #
    # `admission_enabled` exists for one purpose — turning the whole mechanism
    # off during an incident without a deploy. It is not a per-environment
    # convenience, and leaving it false in production removes every protection
    # in this entry at once.
    admission_enabled: bool = True

    rate_limit_auth_per_minute: int = 10
    #: Login attempts from ONE source address. Deliberately looser than the
    #: per-identity limit: one address legitimately carries a whole office
    #: behind NAT, while one identity legitimately carries one person.
    rate_limit_auth_per_source_ip_per_minute: int = 30
    rate_limit_analysis_per_minute: int = 10
    rate_limit_optimization_per_minute: int = 6
    rate_limit_scenario_per_minute: int = 20

    max_active_analyses_per_user: int = 1
    max_active_optimizations_per_user: int = 2
    max_active_scenarios_per_user: int = 3
    max_active_document_processing_per_user: int = 2

    # Platform-wide ceilings. Deliberately not readable by ordinary callers:
    # a tenant learning the global cap learns how much traffic it takes to
    # deny service to everyone else.
    global_max_active_optimizations: int = 50
    global_max_active_scenarios: int = 100
    global_max_active_analyses: int = 200
    global_max_active_document_processing: int = 40

    #: How long an admitted job may hold its slot before the lease lapses and
    #: the quota returns on its own. Must exceed the slowest legitimate run of
    #: the operation, or a long job would have its slot reclaimed while it is
    #: still working.
    admission_lease_seconds: int = 900

    #: Secret for the keyed digests that stand in for an email address or a
    #: source address in `admission.rate_counter`. DEDICATED rather than a reuse
    #: of `jwt_secret`: the two have different blast radii and different rotation
    #: schedules, and a signing key that has also been used as a digest key
    #: cannot be rotated without silently resetting every throttle counter.
    #: Rotating this one IS safe — it only re-partitions counters that expire
    #: within a minute anyway.
    admission_identity_secret: str = Field(default="dev-insecure-change-me")

    # --- object storage ---
    s3_endpoint_url: str | None = None
    s3_bucket_documents: str = "onyx-documents"
    s3_bucket_legislation: str = "onyx-legislation"   # TKMS raw imports + extracted text

    # --- ai ---
    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-4-8"

    @field_validator(
        "rate_limit_auth_per_minute",
        "rate_limit_auth_per_source_ip_per_minute",
        "rate_limit_analysis_per_minute",
        "rate_limit_optimization_per_minute",
        "rate_limit_scenario_per_minute",
        "max_active_analyses_per_user",
        "max_active_optimizations_per_user",
        "max_active_scenarios_per_user",
        "max_active_document_processing_per_user",
        "global_max_active_optimizations",
        "global_max_active_scenarios",
        "global_max_active_analyses",
        "global_max_active_document_processing",
        "admission_lease_seconds",
    )
    @classmethod
    def _positive(cls, value: int, info: ValidationInfo) -> int:
        """A limit of zero would admit nothing, and a negative one is
        meaningless. Both are startup failures rather than a service that
        refuses every request or one that silently never limits."""
        if value <= 0:
            raise ValueError(
                f"{info.field_name} must be a positive integer (got {value}); "
                "to disable a control, change the policy registry, not the limit"
            )
        return value

    @model_validator(mode="after")
    def _per_user_within_global(self) -> Settings:
        """A per-user cap above the platform cap could never be reached, which
        makes it a limit that looks enforced and is not."""
        pairs = (
            ("max_active_optimizations_per_user", "global_max_active_optimizations"),
            ("max_active_scenarios_per_user", "global_max_active_scenarios"),
            ("max_active_analyses_per_user", "global_max_active_analyses"),
            ("max_active_document_processing_per_user",
             "global_max_active_document_processing"),
        )
        for per_user, global_cap in pairs:
            if getattr(self, per_user) > getattr(self, global_cap):
                raise ValueError(
                    f"{per_user} ({getattr(self, per_user)}) exceeds "
                    f"{global_cap} ({getattr(self, global_cap)}), so the "
                    "per-user limit could never be reached"
                )
        return self

    @model_validator(mode="after")
    def _identity_secret_is_real_in_production(self) -> Settings:
        """The dev default must not reach production.

        `admission_identity_secret` is what stops `admission.rate_counter` from
        being a searchable list of the email addresses people tried to log in
        with. Left at the compiled-in default it is public, so the digests are
        reversible by anyone with the source — which is everyone. Checked HERE,
        at startup, rather than trusted to a deployment checklist: a checklist
        failure is silent and this one is loud.

        Development and test keep the default on purpose; a required secret in
        every local shell buys nothing and gets pasted into a repository.
        """
        if self.environment == "production":
            if self.admission_identity_secret == "dev-insecure-change-me":
                raise ValueError(
                    "ONYX_ADMISSION_IDENTITY_SECRET is still the development "
                    "default in production; set it to a generated secret"
                )
            if len(self.admission_identity_secret) < 32:
                raise ValueError(
                    "ONYX_ADMISSION_IDENTITY_SECRET must be at least 32 "
                    "characters; a short key is brute-forceable against a "
                    "known email address"
                )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
