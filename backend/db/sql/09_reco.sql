-- =============================================================================
-- Onyx Ledger — 09 · Recommendation Engine  (schema: reco)
-- Alembic revision: 0010_reco
-- Optimization opportunities produced by an analysis, each citing the rule
-- version that generated it. Lifecycle is an event log; current status cached.
-- =============================================================================

CREATE TABLE reco.recommendation (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    analysis_id         uuid NOT NULL REFERENCES analysis.analysis_run(id) ON DELETE CASCADE,
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    tax_rule_version_id uuid REFERENCES tax_kb.tax_rule_version(id),   -- citation
    opportunity_code    text NOT NULL,               -- 'rrsp','fhsa',...
    category            text,                        -- 'Registered accounts',...
    title               text NOT NULL,
    mechanism           text,
    where_text          text,
    how_text            text,
    why_text            text,
    estimated_impact    ref.money_amt,
    impact_label        text,
    confidence_score    smallint CHECK (confidence_score BETWEEN 0 AND 100),
    priority            smallint NOT NULL DEFAULT 3,
    citation            text,
    status              text NOT NULL DEFAULT 'generated'
                        CHECK (status IN ('generated','viewed','accepted','rejected','completed')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_reco_user ON reco.recommendation (user_id, created_at DESC);
CREATE INDEX ix_reco_analysis ON reco.recommendation (analysis_id);
CREATE INDEX ix_reco_status ON reco.recommendation (status);

-- Lifecycle as an append-only event log (source of truth for status timing).
CREATE TABLE reco.recommendation_status_event (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    recommendation_id   uuid NOT NULL REFERENCES reco.recommendation(id) ON DELETE CASCADE,
    status              text NOT NULL CHECK (status IN ('generated','viewed','accepted','rejected','completed')),
    actor_user_id       uuid REFERENCES identity.user_account(id) ON DELETE SET NULL,
    note                text,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_reco_status_event ON reco.recommendation_status_event (recommendation_id, created_at);

CREATE TABLE reco.recommendation_feedback (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    recommendation_id   uuid NOT NULL REFERENCES reco.recommendation(id) ON DELETE CASCADE,
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    rating              smallint CHECK (rating BETWEEN 1 AND 5),
    comment             text,
    created_at          timestamptz NOT NULL DEFAULT now()
);
