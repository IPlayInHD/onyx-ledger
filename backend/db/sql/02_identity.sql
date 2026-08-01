-- =============================================================================
-- Onyx Ledger — 02 · Identity & User Management  (schema: identity)
-- Alembic revision: 0003_identity
-- Hub A root: identity.user_account. Credentials/tokens split out; secrets
-- stored ONLY as hashes or KMS references — never plaintext.
-- =============================================================================

CREATE TABLE identity.user_account (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    email           citext NOT NULL,
    status          text NOT NULL DEFAULT 'pending_verification'
                    CHECK (status IN ('pending_verification','active','suspended','closed')),
    email_verified_at timestamptz,
    last_login_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,                    -- soft delete
    row_version     integer NOT NULL DEFAULT 1
);
-- Email unique only among non-deleted accounts (allows re-registration post-erasure).
CREATE UNIQUE INDEX uq_user_account_email_active
    ON identity.user_account (email) WHERE deleted_at IS NULL;
CREATE INDEX ix_user_account_status ON identity.user_account (status) WHERE deleted_at IS NULL;

-- Credentials isolated from the hot account row.
CREATE TABLE identity.user_credential (
    user_id         uuid PRIMARY KEY REFERENCES identity.user_account(id) ON DELETE CASCADE,
    password_hash   text NOT NULL,                  -- Argon2id
    algorithm       text NOT NULL DEFAULT 'argon2id',
    must_reset      boolean NOT NULL DEFAULT false,
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Sessions: store only a HASH of the refresh token.
CREATE TABLE identity.auth_session (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    refresh_token_hash  text NOT NULL,
    ip_address          inet,
    user_agent          text,
    issued_at           timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL,
    revoked_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_auth_session_user ON identity.auth_session (user_id);
CREATE INDEX ix_auth_session_active ON identity.auth_session (user_id)
    WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX uq_auth_session_token ON identity.auth_session (refresh_token_hash);

-- Append-only login history (success + failure) for security analytics.
CREATE TABLE identity.login_event (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id     uuid REFERENCES identity.user_account(id) ON DELETE SET NULL,
    email_tried citext,                             -- captured even if user unknown
    event_type  text NOT NULL CHECK (event_type IN ('success','failure','locked','mfa_challenge')),
    ip_address  inet,
    user_agent  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_login_event_user_time ON identity.login_event (user_id, created_at DESC);

CREATE TABLE identity.password_reset_token (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id     uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    token_hash  text NOT NULL UNIQUE,               -- hash only
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_pwreset_user ON identity.password_reset_token (user_id);

CREATE TABLE identity.email_verification_token (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id     uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    token_hash  text NOT NULL UNIQUE,
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- MFA readiness. Secrets are KMS REFERENCES, not raw TOTP seeds.
CREATE TABLE identity.mfa_method (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id         uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    method_type     text NOT NULL CHECK (method_type IN ('totp','webauthn','sms')),
    secret_kms_ref  text,                           -- pointer into external KMS
    label           text,
    confirmed_at    timestamptz,
    disabled_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_mfa_user ON identity.mfa_method (user_id) WHERE disabled_at IS NULL;
