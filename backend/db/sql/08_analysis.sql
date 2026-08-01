-- =============================================================================
-- Onyx Ledger — 08 · Tax Analysis Results  (schema: analysis)
-- Alembic revision: 0009_analysis
-- The bridge: a user snapshot meets the law. Analyses are IMMUTABLE historical
-- facts; derived aggregates are persisted so results never drift when the
-- engine changes. Every line/check traces to the rule version that produced it.
-- =============================================================================

CREATE TABLE analysis.analysis_run (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    tax_year            ref.tax_year_num NOT NULL REFERENCES ref.tax_year(year),
    province_code       text REFERENCES ref.province(code),
    engine_version      text NOT NULL,               -- e.g. '1.0.0'
    status              text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','completed','failed')),
    -- persisted derived results (immutable once completed)
    total_income        ref.money_amt,
    taxable_income      ref.money_amt,
    estimated_tax       ref.money_amt,
    estimated_savings   ref.money_amt,
    marginal_rate       ref.rate,
    average_rate        ref.rate,
    confidence_score    smallint CHECK (confidence_score BETWEEN 0 AND 100),
    data_verified       boolean NOT NULL DEFAULT false,
    started_at          timestamptz,
    completed_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_analysis_user_year ON analysis.analysis_run (user_id, tax_year, created_at DESC);
CREATE INDEX ix_analysis_status ON analysis.analysis_run (status);

-- Immutable, point-in-time inputs (JSONB justified: frozen arbitrary snapshot).
CREATE TABLE analysis.analysis_input_snapshot (
    analysis_id     uuid PRIMARY KEY REFERENCES analysis.analysis_run(id) ON DELETE CASCADE,
    snapshot        jsonb NOT NULL,
    snapshot_hash   text NOT NULL,                   -- integrity / dedupe
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- The computed breakdown (income / deduction / credit / tax lines).
CREATE TABLE analysis.analysis_line_item (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    analysis_id         uuid NOT NULL REFERENCES analysis.analysis_run(id) ON DELETE CASCADE,
    kind                text NOT NULL CHECK (kind IN ('income','deduction','credit','tax','payable')),
    label               text NOT NULL,
    amount              ref.money_amt NOT NULL,
    fact_key            text REFERENCES rules.fact_definition(fact_key),
    tax_rule_version_id uuid REFERENCES tax_kb.tax_rule_version(id),  -- explainability
    sort_order          smallint NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_line_item_analysis ON analysis.analysis_line_item (analysis_id, kind);

-- Assurance layer: auditor-style reconciliation checks (pass/review/flag).
CREATE TABLE analysis.reconciliation_check (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    analysis_id uuid NOT NULL REFERENCES analysis.analysis_run(id) ON DELETE CASCADE,
    check_code  text NOT NULL,                       -- 'cpp','ei','tie-out',...
    status      text NOT NULL CHECK (status IN ('pass','review','flag')),
    label       text NOT NULL,
    detail      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_recon_analysis ON analysis.reconciliation_check (analysis_id, status);

-- Personalized modelling assumptions applied to this analysis.
CREATE TABLE analysis.analysis_assumption (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    analysis_id uuid NOT NULL REFERENCES analysis.analysis_run(id) ON DELETE CASCADE,
    text        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
