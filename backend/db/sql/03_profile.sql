-- =============================================================================
-- Onyx Ledger — 03 · User Financial/Tax Profile  (schema: profile)
-- Alembic revision: 0004_profile
-- Current-state facts about the user. Point-in-time truth is frozen per
-- analysis in analysis.analysis_input_snapshot (see 08_analysis.sql).
-- =============================================================================

-- Lightweight presentation profile (1:1 with account).
CREATE TABLE profile.user_profile (
    user_id     uuid PRIMARY KEY REFERENCES identity.user_account(id) ON DELETE CASCADE,
    display_name text,
    locale      text NOT NULL DEFAULT 'en-CA',
    timezone    text NOT NULL DEFAULT 'America/Toronto',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Tax profile (1:1). Personal + employment + housing facts used by the engine.
CREATE TABLE profile.tax_profile (
    user_id             uuid PRIMARY KEY REFERENCES identity.user_account(id) ON DELETE CASCADE,
    date_of_birth       date,
    province_code       text REFERENCES ref.province(code),
    residency_status    text REFERENCES ref.residency_status(code),
    marital_status      text REFERENCES ref.marital_status(code),
    is_student          boolean NOT NULL DEFAULT false,
    has_disability      boolean NOT NULL DEFAULT false,
    first_time_home_buyer boolean NOT NULL DEFAULT false,
    -- employment
    employment_type     text REFERENCES ref.employment_type(code),
    industry            text,
    employer_name       text,
    is_self_employed    boolean NOT NULL DEFAULT false,
    -- housing
    housing_status      text REFERENCES ref.housing_status(code),
    owns_home           boolean NOT NULL DEFAULT false,
    has_mortgage        boolean NOT NULL DEFAULT false,
    -- situational flags (drive scope checks in the assurance layer)
    has_investments     boolean NOT NULL DEFAULT false,
    has_rental_income   boolean NOT NULL DEFAULT false,
    has_foreign_income  boolean NOT NULL DEFAULT false,
    has_crypto          boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    row_version         integer NOT NULL DEFAULT 1
);
CREATE INDEX ix_tax_profile_province ON profile.tax_profile (province_code);

-- Dependents (multi-valued → own table).
CREATE TABLE profile.dependent (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id         uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    relationship    text NOT NULL DEFAULT 'child'
                    CHECK (relationship IN ('child','parent','grandparent','other')),
    date_of_birth   date,
    has_disability  boolean NOT NULL DEFAULT false,
    is_eligible_dependant boolean NOT NULL DEFAULT true,
    net_income      ref.money_amt,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX ix_dependent_user ON profile.dependent (user_id) WHERE deleted_at IS NULL;

-- Spouse facts (1:1). Modeled as data on the filer, not a linked account.
CREATE TABLE profile.spouse_profile (
    user_id         uuid PRIMARY KEY REFERENCES identity.user_account(id) ON DELETE CASCADE,
    has_spouse      boolean NOT NULL DEFAULT false,
    spouse_net_income ref.money_amt,
    spouse_date_of_birth date,
    spouse_is_student boolean NOT NULL DEFAULT false,
    spouse_has_disability boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Evolving UI/notification preferences → JSONB (justified: open, per-user shape).
CREATE TABLE profile.user_preference (
    user_id             uuid PRIMARY KEY REFERENCES identity.user_account(id) ON DELETE CASCADE,
    notification_prefs  jsonb NOT NULL DEFAULT '{}'::jsonb,
    ui_prefs            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Privacy / consent settings (structured; consent EVENTS live in audit.consent_log).
CREATE TABLE profile.user_privacy_setting (
    user_id                 uuid PRIMARY KEY REFERENCES identity.user_account(id) ON DELETE CASCADE,
    marketing_consent       boolean NOT NULL DEFAULT false,
    analytics_consent       boolean NOT NULL DEFAULT false,
    data_retention_years    smallint NOT NULL DEFAULT 7,
    consent_version         text,
    consented_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
