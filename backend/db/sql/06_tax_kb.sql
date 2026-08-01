-- =============================================================================
-- Onyx Ledger — 06 · Tax Knowledge Base  (schema: tax_kb)
-- Alembic revision: 0007_tax_kb
-- Hub B: versioned Canadian tax law as DATA. Rules are append-only; a rule's
-- identity is stable, each year/amendment is a new immutable version.
-- =============================================================================

-- Government source + legislation references (normalized, reusable).
CREATE TABLE tax_kb.gov_source (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    name        text NOT NULL,                       -- 'Canada Revenue Agency'
    url         text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tax_kb.legislation_reference (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    citation    text NOT NULL,                       -- 'Income Tax Act s.118.2'
    title       text,
    url         text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Stable rule identity (the "what", independent of any year).
CREATE TABLE tax_kb.tax_rule (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code            text NOT NULL UNIQUE,            -- 'MEDICAL_EXPENSE_CREDIT'
    name            text NOT NULL,
    category        text NOT NULL REFERENCES ref.rule_category(code),
    jurisdiction_id uuid NOT NULL REFERENCES ref.jurisdiction(id),
    province_code   text REFERENCES ref.province(code),  -- NULL = federal / all
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_tax_rule_category ON tax_kb.tax_rule (category);
CREATE INDEX ix_tax_rule_jurisdiction ON tax_kb.tax_rule (jurisdiction_id);

-- Immutable versions (bitemporal: valid-time via effective/expiry + tax_year;
-- transaction-time via status lifecycle). NEVER overwrite; supersede instead.
CREATE TABLE tax_kb.tax_rule_version (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    tax_rule_id         uuid NOT NULL REFERENCES tax_kb.tax_rule(id) ON DELETE RESTRICT,
    tax_year            ref.tax_year_num NOT NULL REFERENCES ref.tax_year(year),
    effective_date      date NOT NULL,
    expiry_date         date,                        -- NULL = open-ended
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','pending_approval','approved','published','superseded','retired')),
    description         text NOT NULL,
    ai_explanation      text,                        -- plain-language explanation
    max_amount          ref.money_amt,
    min_amount          ref.money_amt,
    income_threshold_low  ref.money_amt,
    income_threshold_high ref.money_amt,
    reduction_rate      ref.rate,                    -- e.g. 3% medical floor
    formula_id          uuid,                        -- FK to rules.calc_formula (added in 07)
    gov_source_id       uuid REFERENCES tax_kb.gov_source(id),
    source_url          text,
    legislation_reference_id uuid REFERENCES tax_kb.legislation_reference(id),
    superseded_by_version_id uuid REFERENCES tax_kb.tax_rule_version(id),
    published_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (expiry_date IS NULL OR expiry_date >= effective_date)
);
-- Exactly one PUBLISHED version per rule per tax_year.
CREATE UNIQUE INDEX uq_rule_version_published
    ON tax_kb.tax_rule_version (tax_rule_id, tax_year)
    WHERE status = 'published';
CREATE INDEX ix_rule_version_rule ON tax_kb.tax_rule_version (tax_rule_id, tax_year);
CREATE INDEX ix_rule_version_status ON tax_kb.tax_rule_version (status);
CREATE INDEX ix_rule_version_effective ON tax_kb.tax_rule_version (effective_date, expiry_date);

-- Progressive tax brackets: inherently tabular → stored as rows, not a formula.
CREATE TABLE tax_kb.tax_bracket_set (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    jurisdiction_id uuid NOT NULL REFERENCES ref.jurisdiction(id),
    tax_year        ref.tax_year_num NOT NULL REFERENCES ref.tax_year(year),
    kind            text NOT NULL DEFAULT 'income_tax'
                    CHECK (kind IN ('income_tax','surtax')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (jurisdiction_id, tax_year, kind)
);

CREATE TABLE tax_kb.tax_bracket (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    bracket_set_id  uuid NOT NULL REFERENCES tax_kb.tax_bracket_set(id) ON DELETE CASCADE,
    ordinal         smallint NOT NULL,
    lower_bound     ref.money_amt NOT NULL,
    upper_bound     ref.money_amt,                   -- NULL = top bracket (∞)
    rate            ref.rate NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bracket_set_id, ordinal)
);
CREATE INDEX ix_tax_bracket_set ON tax_kb.tax_bracket (bracket_set_id, ordinal);

-- Contribution limits (RRSP/TFSA/FHSA/RESP) per year.
CREATE TABLE tax_kb.contribution_limit (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    registered_type     text NOT NULL REFERENCES ref.account_registered_type(code),
    tax_year            ref.tax_year_num NOT NULL REFERENCES ref.tax_year(year),
    annual_limit        ref.money_amt,
    lifetime_limit      ref.money_amt,
    percent_of_income   ref.rate,                    -- RRSP = 0.18
    allows_carryforward boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (registered_type, tax_year)
);

-- Government benefit programs (GST/HST credit, CCB, CWB) + parameters per year.
CREATE TABLE tax_kb.benefit_program (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code            text NOT NULL UNIQUE,            -- 'CCB','GST_CREDIT','CWB'
    name            text NOT NULL,
    jurisdiction_id uuid NOT NULL REFERENCES ref.jurisdiction(id),
    is_refundable   boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tax_kb.benefit_parameter (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    benefit_program_id  uuid NOT NULL REFERENCES tax_kb.benefit_program(id) ON DELETE CASCADE,
    tax_year            ref.tax_year_num NOT NULL REFERENCES ref.tax_year(year),
    param_key           text NOT NULL,               -- 'base_amount','phase_out_rate'
    param_value         ref.money_amt,
    param_rate          ref.rate,
    formula_id          uuid,                         -- FK to rules.calc_formula (added in 07)
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (benefit_program_id, tax_year, param_key)
);
