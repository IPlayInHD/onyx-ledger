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

    # --- launch scope ---
    #
    # WHAT ONYX OFFERS, which is narrower than what the engine CAN compute.
    #
    # These are not a second tax registry. The engine's resolved dataset remains
    # the authority on what is computable, and a test asserts every pair below
    # actually resolves there — so this can only ever NARROW the engine, never
    # claim capability it does not have.
    #
    # Federal is implicit in every Canadian return and is not listed. Alberta
    # and British Columbia are computable but rest partly on constants resident
    # in the engine rather than governed published brackets; Quebec needs QPP and
    # QPIP handling that does not exist. None of the three is offered.
    launch_tax_years: tuple[int, ...] = (2025, 2026)
    launch_provinces: tuple[str, ...] = ("ON",)

    # --- database (async URL for the app; sync URL for Alembic) ---
    database_url: str = "postgresql+asyncpg://onyx_app_rw@localhost:5432/onyx"
    database_url_sync: str = "postgresql+psycopg2://onyx_migrator@localhost:5432/onyx"
    #: The PRIVILEGED privacy-worker connection. Deliberately optional and
    #: deliberately NOT defaulted to `database_url`: the whole point of PD-16's
    #: remediation is that the process able to purge an account is not the
    #: process serving HTTP. A silent fallback would collapse the boundary back
    #: to `onyx_app_rw` on any host where the operator forgot to set it, and it
    #: would do so invisibly. Absent means the privacy worker refuses to run.
    privacy_database_url: str | None = None
    #: The PRIVILEGED freshness-relay connection. Same contract and same reason
    #: as `privacy_database_url`: PD-16 is precisely this capability being
    #: reachable from the application identity.
    freshness_database_url: str | None = None
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
    #: WHICH ADAPTER SERVES DOCUMENT BYTES. `local` is the in-memory store used
    #: by development and the test suite; `s3` is the production adapter.
    #:
    #: This field exists because the choice used to not be a choice:
    #: `get_object_storage()` returned `LocalObjectStorage` unconditionally, so
    #: a production deployment would have served customer documents out of one
    #: process's heap — losing them on restart, never sharing them between
    #: tasks, and, worst of the three, letting the privacy deletion phase report
    #: erasure it had not performed anywhere durable.
    #:
    #: `_production_storage_is_real` below refuses `local` in production, and
    #: the factory refuses it again. Two layers on purpose: a settings guard
    #: protects a process that reads settings, and the factory protects one that
    #: constructs a store some other way.
    storage_provider: str = "local"
    s3_endpoint_url: str | None = None
    #: Required in production. boto3 can infer a region from the environment,
    #: and an inferred region silently pointing at the wrong bucket is exactly
    #: the class of accident this file exists to make loud.
    s3_region: str | None = None
    #: Optional SSE algorithm (e.g. "aws:kms") applied to every PUT. Left unset
    #: the bucket's own default encryption applies, which is the normal
    #: production arrangement; setting it here is belt and braces for a bucket
    #: whose default nobody can vouch for.
    s3_sse_algorithm: str | None = None
    #: KMS key id, only meaningful with `s3_sse_algorithm = "aws:kms"`.
    s3_sse_kms_key_id: str | None = None
    s3_bucket_documents: str = "onyx-documents"
    s3_bucket_legislation: str = "onyx-legislation"   # TKMS raw imports + extracted text

    #: NO STATIC CREDENTIALS LIVE HERE, deliberately. boto3 resolves credentials
    #: through its own chain — task role, instance profile, web identity,
    #: environment — and adding `aws_access_key_id` to this file would create a
    #: place for a long-lived key to be committed to. There is no such field and
    #: there should not be one.

    # --- transactional email ---
    #: `capture` records messages in-process for development and tests; `ses`
    #: is the production adapter. Production refuses `capture` at both the
    #: settings layer and the factory, exactly as `storage_provider` does.
    email_provider: str = "capture"
    #: The From: identity. Must be a verified SES sending identity in
    #: production; there is no sensible default, and a wrong one is a bounce.
    email_sender_address: str | None = None
    ses_region: str | None = None

    #: Where a verification or reset link points. NOT derived from a request
    #: header — `Host` is attacker-controlled, and a reset link whose host a
    #: caller can choose is a password-reset token delivered to the attacker.
    #: Configuration only, https in production.
    app_public_url: str | None = None

    #: Recovery token lifetimes. Short enough that a link sitting in a mailbox
    #: stops being a credential quickly; long enough to survive a customer who
    #: reads their mail after lunch.
    verification_token_ttl_minutes: int = 60 * 24
    password_reset_token_ttl_minutes: int = 60

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
    def _production_secrets_are_real(self) -> Settings:
        """The dev defaults must not reach production.

        Two independent secrets share this guard because they share the failure
        mode: a compiled-in default that is public in the source, silently
        shipped because an env var was left unset. Checked HERE, at startup,
        rather than trusted to a deployment checklist — a checklist failure is
        silent and this one is loud.

        `jwt_secret` signs and verifies every access token and, through the
        admin scope, every admin-plane token. Left at the compiled default it is
        a source-public HMAC key, so anyone can forge a token for any `sub` and
        mint `scope:"admin"` tokens — total authentication bypass and privilege
        escalation. It is the highest-value secret in the system and MUST carry
        at least the same production guard as the lower-blast-radius digest key
        below.

        `admission_identity_secret` is what stops `admission.rate_counter` from
        being a searchable list of the email addresses people tried to log in
        with. Left at the default it is public, so the digests are reversible by
        anyone with the source.

        Development and test keep the defaults on purpose; a required secret in
        every local shell buys nothing and gets pasted into a repository.
        """
        if self.environment == "production":
            for value, env_var in (
                (self.jwt_secret, "ONYX_JWT_SECRET"),
                (self.admission_identity_secret, "ONYX_ADMISSION_IDENTITY_SECRET"),
            ):
                if value == "dev-insecure-change-me":
                    raise ValueError(
                        f"{env_var} is still the development default in "
                        "production; set it to a generated secret"
                    )
                if len(value) < 32:
                    raise ValueError(
                        f"{env_var} must be at least 32 characters; a short key "
                        "is brute-forceable"
                    )
        return self

    @model_validator(mode="after")
    def _production_storage_is_real(self) -> Settings:
        """Production must not serve customer documents from a process heap.

        `LocalObjectStorage` keeps objects in two module-level dicts. In
        production that would mean: bytes lost on every restart and every deploy,
        invisible to any other task or worker, and — the reason this is a
        privacy guard and not merely an availability one — a deletion phase that
        pops a key out of a dict and truthfully reports `DELETED` while the
        customer's document was never anywhere durable to begin with. An erasure
        record that describes a store nobody wrote to is worse than no record.

        So the value is checked at startup, where the failure is loud, rather
        than trusted to a deployment checklist, where it is silent. Same
        reasoning as `_production_secrets_are_real` above, and the same shape.

        WHAT IS NOT CHECKED HERE: credentials. boto3 resolves them through its
        own chain — task role, instance profile, web identity — and asserting
        their presence at import time would either require static keys in
        configuration (which is the thing to avoid) or duplicate a resolution
        this process does not own. A missing credential surfaces as a
        `PERMANENT_FAILURE` from the adapter, which the privacy lifecycle
        already refuses to treat as erasure.
        """
        if self.environment != "production":
            return self

        if self.storage_provider != "s3":
            raise ValueError(
                f"ONYX_STORAGE_PROVIDER is {self.storage_provider!r} in "
                "production; customer documents must not be served from an "
                "in-memory store. Set it to 's3'."
            )
        if not self.s3_region:
            raise ValueError(
                "ONYX_S3_REGION must be set in production; an inferred region "
                "can point at a bucket nobody intended"
            )
        for field, env_var in (
            ("s3_bucket_documents", "ONYX_S3_BUCKET_DOCUMENTS"),
            ("s3_bucket_legislation", "ONYX_S3_BUCKET_LEGISLATION"),
        ):
            # The DEFAULT is the problem, not emptiness: "onyx-documents" is a
            # plausible bucket name, so a deployment that never set it would
            # sail through a non-empty check and then read and write somebody
            # else's bucket, or none.
            if field not in self.model_fields_set:
                raise ValueError(
                    f"{env_var} must be set explicitly in production; the "
                    "compiled default is a guess, not a bucket"
                )
        return self

    @model_validator(mode="after")
    def _production_email_is_real(self) -> Settings:
        """Production must not file its own recovery mail in a list.

        `CaptureEmailProvider` appends to a module-level list and opens no
        socket. In production that means: every verification email captured,
        every password-reset email captured, and an application that reports
        success for both. Nothing errors. The only signal is customers who
        cannot get in and cannot say why — which is the same failure shape as
        the in-memory object store, and is checked here for the same reason.

        THE PUBLIC URL IS PART OF THIS. A verification link is built from it,
        so an unset value in production would send customers to nowhere, and a
        plain-http value would put a single-use credential in a URL that
        travels in cleartext.

        NOT CHECKED HERE: AWS credentials. boto3 resolves them through its own
        chain, and asserting them at import time would need static keys in
        configuration — the thing to avoid. A missing credential surfaces as a
        permanent `EmailDeliveryFailed`, which leaves the token usable and the
        resend path open.
        """
        if self.environment != "production":
            return self

        if self.email_provider != "ses":
            raise ValueError(
                f"ONYX_EMAIL_PROVIDER is {self.email_provider!r} in production; "
                "verification and password-reset mail would be captured in "
                "memory instead of delivered. Set it to 'ses'."
            )
        if not self.email_sender_address:
            raise ValueError(
                "ONYX_EMAIL_SENDER_ADDRESS must be set in production and must "
                "be a verified sending identity"
            )
        if not self.ses_region:
            raise ValueError("ONYX_SES_REGION must be set in production")
        if not self.app_public_url:
            raise ValueError(
                "ONYX_APP_PUBLIC_URL must be set in production; verification "
                "and reset links are built from it"
            )
        if not self.app_public_url.startswith("https://"):
            raise ValueError(
                "ONYX_APP_PUBLIC_URL must be https in production; a "
                "single-use recovery token must not travel in a cleartext URL"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
