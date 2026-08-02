-- =============================================================================
-- Onyx Ledger — 21 · Income Optimization Engine  (schema: ioe)
-- Alembic revision: 0027_ioe
-- IOE architecture Revision 2 §7/§8 + Revision 2.1 §A (rule snapshots), §B
-- (objective metrics), §D (assembly limitations), §E (interaction mathematics).
--
-- The IOE is the strategic decision-support layer. It never calculates tax and
-- never interprets legislation: it consumes verified outputs from the tax engine
-- and the rules evaluator and records HOW it transformed them. This schema is
-- therefore mostly PROVENANCE — every score, confidence value, economic effect,
-- portfolio decision, and hash is stored so a result can be explained and
-- verified rather than trusted.
--
-- Two record classes, enforced by different triggers (§8):
--   * MUTABLE WORKFLOW HEADERS  — optimization_run, scenario, weight_config.
--     Status/freshness/visibility transitions only; result columns seal on
--     completion.
--   * IMMUTABLE CALCULATION EVIDENCE — everything else. UPDATE always rejected;
--     DELETE rejected unless an explicit purge context is set (account erasure).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS ioe;

-- =============================================================================
-- Trigger functions (defined first; attached per table below)
-- =============================================================================

-- Immutable calculation evidence: insert-only.
-- DELETE is permitted ONLY inside an explicit purge context, which exists so the
-- account-erasure path (privacy/retention) can run; it is the single sanctioned
-- way to remove evidence and is subject to the SECURITY DEFINER review in §25.
CREATE OR REPLACE FUNCTION ioe.reject_result_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'ioe.% is immutable calculation evidence (UPDATE rejected)',
            TG_TABLE_NAME;
    END IF;
    IF coalesce(current_setting('app.allow_evidence_purge', true), 'off') <> 'on' THEN
        RAISE EXCEPTION 'ioe.% is immutable calculation evidence (DELETE rejected)',
            TG_TABLE_NAME;
    END IF;
    RETURN OLD;
END;
$$;

-- Workflow headers: validate the status transition and seal result columns.
-- Sealed column names are passed as trigger arguments, so one function serves
-- every workflow table.
CREATE OR REPLACE FUNCTION ioe.guard_workflow_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    allowed text[];
    sealed  text;
BEGIN
    IF NEW.workflow_status IS DISTINCT FROM OLD.workflow_status THEN
        allowed := CASE OLD.workflow_status
            WHEN 'pending' THEN ARRAY['running','failed','cancelled']
            WHEN 'running' THEN ARRAY['completed','failed','cancelled']
            ELSE ARRAY[]::text[]          -- completed/failed/cancelled are terminal
        END;
        IF NOT (NEW.workflow_status = ANY (allowed)) THEN
            RAISE EXCEPTION 'ioe.%: illegal workflow transition % -> %',
                TG_TABLE_NAME, OLD.workflow_status, NEW.workflow_status;
        END IF;
    END IF;

    -- Once completed, the computed result is evidence and cannot drift.
    -- Freshness/visibility columns are deliberately NOT sealed (§24).
    IF OLD.workflow_status = 'completed' THEN
        FOREACH sealed IN ARRAY TG_ARGV LOOP
            IF (to_jsonb(NEW) ->> sealed) IS DISTINCT FROM (to_jsonb(OLD) ->> sealed) THEN
                RAISE EXCEPTION 'ioe.%: column "%" is sealed once the record is completed',
                    TG_TABLE_NAME, sealed;
            END IF;
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$;

-- =============================================================================
-- Versioned configuration
-- =============================================================================

-- Ranking weights as versioned DATA (§15 of the brief / Revision 2 §30).
-- The scoring ALGORITHM version is a separate manifest key — never conflated.
CREATE TABLE ioe.weight_config (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    version         text NOT NULL UNIQUE,
    schema_version  text NOT NULL DEFAULT '1',
    weights         jsonb NOT NULL,          -- {factor_code: weight}
    checksum        text NOT NULL,           -- canonical hash of `weights`
    is_active       boolean NOT NULL DEFAULT false,
    activated_by    uuid REFERENCES admin.admin_user(id),
    activated_at    timestamptz,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
-- At most one active configuration at a time.
CREATE UNIQUE INDEX uq_weight_config_active ON ioe.weight_config ((true)) WHERE is_active;

-- =============================================================================
-- Rule snapshots — content-addressed pinning (Revision 2.1 §A)
-- A version id is NOT its content: shared artifacts (formulas, constants) can
-- drift beneath an immutable version id, and the engine's reference data lives
-- in code. Snapshots make that drift detectable instead of silent.
-- =============================================================================

CREATE TABLE ioe.rule_snapshot (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    snapshot_hash   text NOT NULL UNIQUE,    -- content-addressed: shared across runs
    artifact_count  integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ioe.rule_snapshot_artifact (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    snapshot_id     uuid NOT NULL REFERENCES ioe.rule_snapshot(id) ON DELETE CASCADE,
    artifact_kind   text NOT NULL CHECK (artifact_kind IN (
                        'tax_rule_version','calc_formula','calc_formula_input',
                        'rule_condition_group','rule_condition','rule_outcome',
                        'calc_constant','contribution_limit','tax_bracket_set',
                        'tax_bracket','engine_reference_dataset','fact_definition')),
    artifact_id     uuid,                    -- NULL for in-code datasets
    artifact_key    text NOT NULL,           -- stable key, e.g. 'MEDICAL_FLOOR_RATE:2025'
    content_hash    text NOT NULL,
    content         jsonb,                   -- materialized for the high-risk set (D-10)
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, artifact_kind, artifact_key)
);
CREATE INDEX ix_snapshot_artifact_snapshot ON ioe.rule_snapshot_artifact (snapshot_id);

-- =============================================================================
-- Optimization run (WORKFLOW HEADER)
-- =============================================================================

CREATE TABLE ioe.optimization_run (
    id                      uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id                 uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    analysis_id             uuid NOT NULL REFERENCES analysis.analysis_run(id) ON DELETE CASCADE,
    tax_year                ref.tax_year_num NOT NULL REFERENCES ref.tax_year(year),

    workflow_status         text NOT NULL DEFAULT 'pending'
                            CHECK (workflow_status IN
                                ('pending','running','completed','failed','cancelled')),
    -- Freshness is a SEPARATE axis: a completed run may go stale without any of
    -- its calculation evidence changing (§24).
    freshness_status        text NOT NULL DEFAULT 'current'
                            CHECK (freshness_status IN ('current','stale','superseded')),
    evaluated_at            timestamptz,
    stale_at                timestamptz,
    stale_reason_codes      text[] NOT NULL DEFAULT '{}',
    superseded_by_run_id    uuid REFERENCES ioe.optimization_run(id),
    previous_attempt_run_id uuid REFERENCES ioe.optimization_run(id),

    -- identity + pinning (§9)
    optimization_spec_hash  text,
    optimization_result_hash text,
    rule_snapshot_id        uuid REFERENCES ioe.rule_snapshot(id),
    weight_config_id        uuid REFERENCES ioe.weight_config(id),
    version_manifest        jsonb,
    manifest_hash           text,

    -- totals (sealed on completion; the headline comes from the portfolio run)
    portfolio_total_benefit ref.money_amt,
    total_estimated_savings ref.money_amt,

    idempotency_key         text,
    error_code              text,            -- sanitized enum; never a stack trace
    started_at              timestamptz,
    completed_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ioe_run_user_year ON ioe.optimization_run (user_id, tax_year, created_at DESC);
CREATE INDEX ix_ioe_run_status ON ioe.optimization_run (workflow_status);
CREATE INDEX ix_ioe_run_analysis ON ioe.optimization_run (analysis_id);

-- Idempotency (§23): an explicit client key, and a DB-enforced uniqueness on the
-- calculation SPEC so concurrent equivalent requests cannot both create a run.
CREATE UNIQUE INDEX uq_ioe_run_idempotency
    ON ioe.optimization_run (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX uq_ioe_run_spec_current
    ON ioe.optimization_run (user_id, optimization_spec_hash)
    WHERE optimization_spec_hash IS NOT NULL
      AND workflow_status IN ('pending','running','completed')
      AND freshness_status = 'current';

-- Append-only status log.
CREATE TABLE ioe.optimization_run_event (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    run_id          uuid NOT NULL REFERENCES ioe.optimization_run(id) ON DELETE CASCADE,
    from_status     text,
    to_status       text NOT NULL,
    reason_code     text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ioe_run_event ON ioe.optimization_run_event (run_id, created_at);

-- The exact published rule versions in play (human-readable companion to the
-- content-addressed snapshot).
CREATE TABLE ioe.run_rule_version (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    run_id              uuid NOT NULL REFERENCES ioe.optimization_run(id) ON DELETE CASCADE,
    tax_rule_version_id uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, tax_rule_version_id)
);

-- =============================================================================
-- Structured assumptions (§15) — calculations depend on STRUCTURED fields only;
-- `display_note` is presentation and is excluded from hashes.
-- =============================================================================

CREATE TABLE ioe.assumption_set (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    set_hash        text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ioe.assumption (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    assumption_set_id   uuid NOT NULL REFERENCES ioe.assumption_set(id) ON DELETE CASCADE,
    code                text NOT NULL,          -- from the versioned assumption registry
    value               jsonb NOT NULL,
    source              text NOT NULL CHECK (source IN ('user','platform','analysis')),
    certainty           text NOT NULL CHECK (certainty IN
                            ('user_asserted','platform_default','derived_from_data','statutory_known')),
    effective_period    text,
    materiality         text NOT NULL DEFAULT 'medium'
                        CHECK (materiality IN ('high','medium','low')),
    affects_eligibility boolean NOT NULL DEFAULT false,
    sensitivity         ref.rate,               -- measured, not guessed (§14)
    display_note        text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (assumption_set_id, code)
);

-- =============================================================================
-- Candidates and their evidence (IMMUTABLE)
-- =============================================================================

CREATE TABLE ioe.optimization_candidate (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    run_id              uuid NOT NULL REFERENCES ioe.optimization_run(id) ON DELETE CASCADE,
    recommendation_id   uuid REFERENCES reco.recommendation(id) ON DELETE SET NULL,
    opportunity_code    text NOT NULL,
    tax_rule_version_id uuid REFERENCES tax_kb.tax_rule_version(id),

    -- supplied by the rules layer (contract v2) — never inferred here
    eligibility_status  text NOT NULL CHECK (eligibility_status IN
                            ('eligible','conditionally_eligible','ineligible','indeterminate')),
    calculation_basis   text CHECK (calculation_basis IS NULL OR calculation_basis IN
                            ('engine_determined','rule_formula_determined',
                             'scenario_estimate','projection_estimate')),
    -- independent axis: how well the INPUTS are supported (§13)
    evidence_status     text CHECK (evidence_status IS NULL OR evidence_status IN
                            ('documented_verified','documented_unverified',
                             'user_attested','incomplete')),

    -- the three distinct benefit measures (§12.2) — never summed for display
    standalone_potential            ref.money_amt,
    incremental_portfolio_benefit   ref.money_amt,

    portfolio_membership text NOT NULL DEFAULT 'excluded_not_evaluable'
                        CHECK (portfolio_membership IN
                            ('selected','excluded_conflict','excluded_constraint',
                             'excluded_not_evaluable','deferred_timing',
                             'deferred_pending_combination')),
    exclusion_reason_code text,
    candidate_rank      integer,
    recommendation_score numeric(6,2),
    confidence_score    smallint CHECK (confidence_score BETWEEN 0 AND 100),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ioe_candidate_run ON ioe.optimization_candidate (run_id, candidate_rank);

-- A candidate may carry SEVERAL effects (e.g. an RRSP contribution is both a
-- current-year reduction and a deferral). They are never collapsed.
CREATE TABLE ioe.candidate_economic_effect (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    candidate_id        uuid NOT NULL REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    effect_type         text NOT NULL CHECK (effect_type IN (
                            'immediate_refund_impact','current_year_tax_reduction','tax_deferral',
                            'refundable_benefit','recurring_annual_benefit',
                            'multi_year_projected_benefit','future_option_value')),
    amount              ref.money_amt NOT NULL,
    tax_year            ref.tax_year_num,
    horizon_years       smallint NOT NULL DEFAULT 1,
    calculation_basis   text NOT NULL CHECK (calculation_basis IN
                            ('engine_determined','rule_formula_determined',
                             'scenario_estimate','projection_estimate')),
    reversibility       text CHECK (reversibility IS NULL OR reversibility IN
                            ('reversible','partially_reversible','irreversible')),
    is_permanent        boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ioe_effect_candidate ON ioe.candidate_economic_effect (candidate_id);

-- A retained asset (contribution) is NOT a cost; only expenditure is (§B).
CREATE TABLE ioe.candidate_cost (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    candidate_id    uuid NOT NULL REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    cost_type       text NOT NULL CHECK (cost_type IN
                        ('required_cash_contribution','required_expenditure','implementation_cost')),
    amount          ref.money_amt NOT NULL,
    timing          text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ioe_cost_candidate ON ioe.candidate_cost (candidate_id);

-- Per-factor ranking provenance: the score is explainable, not opaque.
CREATE TABLE ioe.score_component (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    candidate_id        uuid NOT NULL REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    factor_code         text NOT NULL,
    raw_value           numeric(18,6),
    normalized_value    numeric(9,6),
    weight              numeric(9,6),
    contribution        numeric(9,6),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (candidate_id, factor_code)
);

CREATE TABLE ioe.confidence_component (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    candidate_id    uuid NOT NULL REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    factor_code     text NOT NULL,
    value           numeric(9,6),
    weight          numeric(9,6),
    contribution    numeric(9,6),
    reason_code     text,                       -- renders the human explanation
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (candidate_id, factor_code)
);

-- =============================================================================
-- Typed relationships (§16) — replaces free-text pairwise conflicts
-- =============================================================================
CREATE TABLE ioe.recommendation_relationship (
    id                          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    run_id                      uuid NOT NULL REFERENCES ioe.optimization_run(id) ON DELETE CASCADE,
    source_candidate_id         uuid NOT NULL REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    target_candidate_id         uuid NOT NULL REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    relationship_type           text NOT NULL CHECK (relationship_type IN
                                    ('requires','precedes','excludes','substitutes',
                                     'shares_limit','enhances','reduces_value','overlaps')),
    shared_resource_code        text,
    maximum_shared_amount       ref.money_amt,
    measured_delta              ref.money_amt,  -- for enhances/reduces_value
    explanation_code            text NOT NULL,
    resolution_options          jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    CHECK (source_candidate_id <> target_candidate_id)
);
CREATE INDEX ix_ioe_relationship_run ON ioe.recommendation_relationship (run_id);

-- =============================================================================
-- Strategy portfolio (§12) — the ONLY source of a user-facing total
-- =============================================================================
CREATE TABLE ioe.strategy_portfolio (
    id                      uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    run_id                  uuid NOT NULL REFERENCES ioe.optimization_run(id) ON DELETE CASCADE,
    assembly_policy_version text NOT NULL,
    assembly_method         text NOT NULL CHECK (assembly_method IN
                                ('greedy_ranked','greedy_ranked_with_local_improvement')),
    -- there is deliberately NO 'optimal' value: the assembler is rule-based
    optimality_claim        text NOT NULL DEFAULT 'none'
                            CHECK (optimality_claim IN ('none','locally_improved')),

    -- objective actually used for selection (§B)
    objective_metric        text NOT NULL CHECK (objective_metric IN (
                                'current_year_tax_reduction',
                                'current_year_tax_reduction_net_of_expenditure',
                                'net_cash_benefit_current_year',
                                'comparable_value_multi_horizon')),
    objective_version       text NOT NULL,
    objective_value_baseline numeric(18,2),
    objective_value_final    numeric(18,2),

    -- engine-verified figures
    baseline_tax            ref.money_amt NOT NULL,
    portfolio_tax           ref.money_amt NOT NULL,
    portfolio_total_benefit ref.money_amt NOT NULL,     -- THE headline number

    -- diagnostics only — sum_of_standalone must never be displayed as a total
    sum_of_standalone       ref.money_amt,
    interaction_delta       ref.money_amt,
    additivity_class        text CHECK (additivity_class IN
                                ('additive','sub_additive','super_additive')),
    additivity_verified     boolean NOT NULL DEFAULT false,
    attribution_method      text NOT NULL DEFAULT 'incremental_path_dependent'
                            CHECK (attribution_method IN
                                ('incremental_path_dependent','shapley_exact')),
    attribution_method_version text,

    -- reported alongside the headline, never added to it
    total_required_cash_contribution ref.money_amt,
    total_required_expenditure       ref.money_amt,
    total_implementation_cost        ref.money_amt,
    net_current_year_benefit         ref.money_amt,
    total_deferral_amount            ref.money_amt,
    total_recurring_annual           ref.money_amt,
    total_multi_year_projected       ref.money_amt,

    deferred_count          integer NOT NULL DEFAULT 0,
    excluded_count          integer NOT NULL DEFAULT 0,
    improvement_moves_applied integer NOT NULL DEFAULT 0,
    improvement_runs_used   integer NOT NULL DEFAULT 0,
    unexplored_alternatives_count integer NOT NULL DEFAULT 0,
    engine_runs_used        integer NOT NULL DEFAULT 0,

    portfolio_result_hash   text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id)
);

CREATE TABLE ioe.portfolio_member (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    portfolio_id        uuid NOT NULL REFERENCES ioe.strategy_portfolio(id) ON DELETE CASCADE,
    candidate_id        uuid NOT NULL REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    apply_order         smallint NOT NULL,
    incremental_benefit ref.money_amt NOT NULL,
    resource_allocations jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (portfolio_id, apply_order),
    UNIQUE (portfolio_id, candidate_id)
);

-- The anti-double-counting ledger: a shared pool can be allocated once.
CREATE TABLE ioe.resource_ledger_entry (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    portfolio_id    uuid NOT NULL REFERENCES ioe.strategy_portfolio(id) ON DELETE CASCADE,
    resource_code   text NOT NULL,
    pool_scope      text NOT NULL DEFAULT 'individual'
                    CHECK (pool_scope IN ('individual','household')),
    capacity        ref.money_amt,
    allocated       ref.money_amt NOT NULL DEFAULT 0,
    remaining       ref.money_amt,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (portfolio_id, resource_code)
);

-- =============================================================================
-- Multi-year projections (§18) — educational, never predictive
-- =============================================================================
CREATE TABLE ioe.multi_year_projection (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    run_id              uuid NOT NULL REFERENCES ioe.optimization_run(id) ON DELETE CASCADE,
    candidate_id        uuid REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    horizon_year        ref.tax_year_num NOT NULL,
    projected_amount    ref.money_amt NOT NULL,
    effect_type         text NOT NULL,
    calculation_basis   text NOT NULL DEFAULT 'projection_estimate'
                        CHECK (calculation_basis = 'projection_estimate'),
    assumption_set_id   uuid REFERENCES ioe.assumption_set(id),
    is_indexation_known boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ioe_projection_run ON ioe.multi_year_projection (run_id, horizon_year);

-- =============================================================================
-- Scenarios (WORKFLOW HEADER + immutable evidence)
-- =============================================================================
CREATE TABLE ioe.scenario (
    id                      uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id                 uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    base_analysis_id        uuid NOT NULL REFERENCES analysis.analysis_run(id) ON DELETE CASCADE,
    label                   text,
    workflow_status         text NOT NULL DEFAULT 'pending'
                            CHECK (workflow_status IN
                                ('pending','running','completed','failed','cancelled')),
    -- "delete" is an ARCHIVE: calculation evidence and audit records are kept
    visibility_status       text NOT NULL DEFAULT 'active'
                            CHECK (visibility_status IN ('active','archived')),
    archived_at             timestamptz,

    scenario_spec_hash      text,
    scenario_result_hash    text,
    rule_snapshot_id        uuid REFERENCES ioe.rule_snapshot(id),
    lever_registry_version  text,
    assumption_set_id       uuid REFERENCES ioe.assumption_set(id),
    version_manifest        jsonb,
    manifest_hash           text,

    idempotency_key         text,
    error_code              text,
    execution_ms            integer,
    started_at              timestamptz,
    completed_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ioe_scenario_user ON ioe.scenario (user_id, created_at DESC);
CREATE INDEX ix_ioe_scenario_analysis ON ioe.scenario (base_analysis_id);
CREATE UNIQUE INDEX uq_ioe_scenario_idempotency
    ON ioe.scenario (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX uq_ioe_scenario_spec
    ON ioe.scenario (user_id, scenario_spec_hash)
    WHERE scenario_spec_hash IS NOT NULL
      AND workflow_status IN ('pending','running','completed')
      AND visibility_status = 'active';

CREATE TABLE ioe.scenario_event (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    scenario_id     uuid NOT NULL REFERENCES ioe.scenario(id) ON DELETE CASCADE,
    from_status     text,
    to_status       text NOT NULL,
    reason_code     text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ioe_scenario_event ON ioe.scenario_event (scenario_id, created_at);

CREATE TABLE ioe.scenario_input_change (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    scenario_id     uuid NOT NULL REFERENCES ioe.scenario(id) ON DELETE CASCADE,
    lever_code      text NOT NULL,
    field           text NOT NULL,          -- resolved by the IOE lever registry
    old_value       text,
    new_value       text,
    apply_order     smallint NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scenario_id, apply_order, field)
);

CREATE TABLE ioe.scenario_result (
    id                      uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    scenario_id             uuid NOT NULL REFERENCES ioe.scenario(id) ON DELETE CASCADE,
    baseline_tax            ref.money_amt NOT NULL,
    scenario_tax            ref.money_amt NOT NULL,
    tax_delta               ref.money_amt NOT NULL,
    net_benefit             ref.money_amt,
    calculation_basis       text NOT NULL DEFAULT 'scenario_estimate'
                            CHECK (calculation_basis = 'scenario_estimate'),
    confidence_score        smallint CHECK (confidence_score BETWEEN 0 AND 100),
    affected_rule_versions  jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scenario_id)
);

CREATE TABLE ioe.run_rule_snapshot (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    run_id          uuid REFERENCES ioe.optimization_run(id) ON DELETE CASCADE,
    scenario_id     uuid REFERENCES ioe.scenario(id) ON DELETE CASCADE,
    snapshot_id     uuid NOT NULL REFERENCES ioe.rule_snapshot(id),
    replay_status   text NOT NULL DEFAULT 'verified'
                    CHECK (replay_status IN ('verified','drifted','unavailable')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(run_id, scenario_id) = 1)
);

-- =============================================================================
-- Triggers
-- =============================================================================

-- updated_at on the mutable workflow tables (15_triggers ran before this schema
-- existed, so attach explicitly).
CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON ioe.optimization_run
    FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();
CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON ioe.scenario
    FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();
CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON ioe.weight_config
    FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();

-- Workflow guards: legal transitions + sealed result columns once completed.
CREATE TRIGGER trg_guard_transition BEFORE UPDATE ON ioe.optimization_run
    FOR EACH ROW EXECUTE FUNCTION ioe.guard_workflow_transition(
        'optimization_spec_hash','optimization_result_hash','rule_snapshot_id',
        'weight_config_id','manifest_hash','portfolio_total_benefit','completed_at');
CREATE TRIGGER trg_guard_transition BEFORE UPDATE ON ioe.scenario
    FOR EACH ROW EXECUTE FUNCTION ioe.guard_workflow_transition(
        'scenario_spec_hash','scenario_result_hash','rule_snapshot_id',
        'lever_registry_version','manifest_hash','completed_at');

-- Immutable calculation evidence + append-only event logs.
DO $$
DECLARE
    immutable_tables text[] := ARRAY[
        'rule_snapshot','rule_snapshot_artifact','run_rule_snapshot','run_rule_version',
        'optimization_run_event','scenario_event',
        'assumption_set','assumption',
        'optimization_candidate','candidate_economic_effect','candidate_cost',
        'score_component','confidence_component','recommendation_relationship',
        'strategy_portfolio','portfolio_member','resource_ledger_entry',
        'multi_year_projection','scenario_input_change','scenario_result'
    ];
    t text;
BEGIN
    FOREACH t IN ARRAY immutable_tables LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_immutable BEFORE UPDATE OR DELETE ON ioe.%I
             FOR EACH ROW EXECUTE FUNCTION ioe.reject_result_mutation();', t);
    END LOOP;
END $$;

-- Audit the privileged workflow records.
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON ioe.optimization_run
    FOR EACH ROW EXECUTE FUNCTION audit.log_change();
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON ioe.scenario
    FOR EACH ROW EXECUTE FUNCTION audit.log_change();
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON ioe.weight_config
    FOR EACH ROW EXECUTE FUNCTION audit.log_change();

-- =============================================================================
-- Row-Level Security
-- RLS is the PRIMARY tenant-isolation control here, reinforced by application
-- authorization, restricted roles, and connection-context safeguards. Following
-- the established pattern, it is applied to the tables that carry user_id;
-- child evidence is reached only through an authorized parent.
-- =============================================================================
ALTER TABLE ioe.optimization_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE ioe.optimization_run FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_optimization_run ON ioe.optimization_run
    USING (user_id = ref.current_app_user())
    WITH CHECK (user_id = ref.current_app_user());

ALTER TABLE ioe.scenario ENABLE ROW LEVEL SECURITY;
ALTER TABLE ioe.scenario FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_scenario ON ioe.scenario
    USING (user_id = ref.current_app_user())
    WITH CHECK (user_id = ref.current_app_user());

-- =============================================================================
-- Grants
-- =============================================================================
GRANT USAGE ON SCHEMA ioe TO onyx_app_rw, onyx_app_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ioe TO onyx_app_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA ioe TO onyx_app_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA ioe
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO onyx_app_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA ioe
    GRANT SELECT ON TABLES TO onyx_app_ro;
