-- =============================================================================
-- Onyx Ledger — 28 · P5 scenario simulation, comparison, archive, freshness
-- Alembic revision: 0034_ioe_scenarios
-- IOE architecture Revision 2 §14–§16, Revision 2.1 §A.
--
-- What P5 adds to the scenario tables laid down in 21_ioe.sql:
--
--   PINNING       every material input is pinned BEFORE the spec is hashed —
--                 the baseline input snapshot AND the baseline RESULT, the rule
--                 snapshot, the objective policy, the registries, and the full
--                 version manifest. A scenario whose baseline result is not
--                 pinned cannot prove what it was measured against.
--   TYPED SPEC    levers arrive as CODES with structured parameters. There is no
--                 column anywhere that could carry a field path, a patch, a
--                 formula, or any other executable mutation data.
--   FRESHNESS     an explicit state plus a structured reason, so a stale result
--                 is labelled rather than silently shown or silently recomputed.
--   SUPERSESSION  refresh creates a NEW scenario and points the old one at it.
--                 A historical result is never mutated to "bring it up to date".
--   ARCHIVE       DELETE is a visibility change. Immutable evidence stays.
--
-- ALL ADDITIVE. No column is dropped or retyped.
-- =============================================================================

-- ---- 1. Pinned specification inputs -----------------------------------------
ALTER TABLE ioe.scenario
    -- what the scenario was measured against, pinned before hashing
    ADD COLUMN baseline_input_snapshot_hash text,
    ADD COLUMN baseline_result_hash         text,
    ADD COLUMN baseline_tax                 ref.money_amt,
    -- comparison compatibility keys; a comparison across differing values is
    -- refused rather than reconciled
    ADD COLUMN tax_year                     integer,
    ADD COLUMN jurisdiction                 text,
    ADD COLUMN objective_code               text,
    ADD COLUMN objective_version            text,
    ADD COLUMN result_schema_version        text,
    -- free text, deliberately OUTSIDE both hashes: renaming a scenario must not
    -- change its identity or invalidate its evidence
    ADD COLUMN note                         text,
    -- freshness
    ADD COLUMN freshness_status             text NOT NULL DEFAULT 'unknown',
    ADD COLUMN stale_reason_code            text,
    ADD COLUMN freshness_evaluated_at       timestamptz,
    -- supersession: refresh produces a NEW scenario
    ADD COLUMN superseded_by_scenario_id    uuid REFERENCES ioe.scenario(id),
    ADD COLUMN refreshed_from_scenario_id   uuid REFERENCES ioe.scenario(id),
    ADD COLUMN superseded_at                timestamptz;

ALTER TABLE ioe.scenario
    ADD CONSTRAINT scenario_freshness_status_check
        CHECK (freshness_status IN (
            'unknown',      -- not yet evaluated
            'current',      -- every pinned input still matches the live world
            'stale',        -- an input moved; the result stands under its pins
            'superseded'    -- a newer scenario replaced this one
        )) NOT VALID,
    ADD CONSTRAINT scenario_stale_reason_check
        CHECK (stale_reason_code IS NULL OR stale_reason_code IN (
            'BASELINE_INPUTS_CHANGED',
            'BASELINE_RESULT_CHANGED',
            'RULE_SNAPSHOT_SUPERSEDED',
            'ENGINE_VERSION_CHANGED',
            'REFERENCE_DATA_CHANGED',
            'LEVER_REGISTRY_CHANGED',
            'OBJECTIVE_POLICY_CHANGED',
            'ASSUMPTION_SET_CHANGED',
            'TAX_YEAR_ROLLED_OVER',
            'SUPERSEDED_BY_REFRESH'
        )) NOT VALID,
    -- a reason exists exactly when the scenario is not current
    ADD CONSTRAINT scenario_stale_reason_iff_not_current
        CHECK ((freshness_status IN ('stale', 'superseded')
                AND stale_reason_code IS NOT NULL)
               OR (freshness_status IN ('unknown', 'current')
                   AND stale_reason_code IS NULL)) NOT VALID,
    ADD CONSTRAINT scenario_superseded_has_target
        CHECK (freshness_status <> 'superseded'
               OR superseded_by_scenario_id IS NOT NULL) NOT VALID,
    -- a scenario may not supersede itself
    ADD CONSTRAINT scenario_supersession_not_self
        CHECK (superseded_by_scenario_id IS DISTINCT FROM id
               AND refreshed_from_scenario_id IS DISTINCT FROM id) NOT VALID,
    ADD CONSTRAINT scenario_archived_at_iff_archived
        CHECK ((visibility_status = 'archived' AND archived_at IS NOT NULL)
               OR (visibility_status = 'active' AND archived_at IS NULL)) NOT VALID;

ALTER TABLE ioe.scenario VALIDATE CONSTRAINT scenario_freshness_status_check;
ALTER TABLE ioe.scenario VALIDATE CONSTRAINT scenario_stale_reason_check;
ALTER TABLE ioe.scenario VALIDATE CONSTRAINT scenario_stale_reason_iff_not_current;
ALTER TABLE ioe.scenario VALIDATE CONSTRAINT scenario_superseded_has_target;
ALTER TABLE ioe.scenario VALIDATE CONSTRAINT scenario_supersession_not_self;
ALTER TABLE ioe.scenario VALIDATE CONSTRAINT scenario_archived_at_iff_archived;

COMMENT ON COLUMN ioe.scenario.baseline_result_hash IS
    'The baseline engine result the scenario was measured against, pinned before the spec hash. Without it a stored delta cannot be shown to be a delta from anything in particular.';
COMMENT ON COLUMN ioe.scenario.label IS
    'User-supplied name. Deliberately excluded from scenario_spec_hash and scenario_result_hash: renaming must not change identity or invalidate evidence.';
COMMENT ON COLUMN ioe.scenario.note IS
    'User-supplied note. Excluded from both hashes, exactly as label is.';
COMMENT ON COLUMN ioe.scenario.freshness_status IS
    'Explicit lifecycle state. A stale result is LABELLED, never silently refreshed and never silently shown as current.';
COMMENT ON COLUMN ioe.scenario.superseded_by_scenario_id IS
    'Set when a refresh produced a newer scenario. The superseded row keeps its own pinned versions and its own result; it is never recomputed in place.';

-- Parent-key-qualified reads are the required access path (P4 closing note:
-- "RLS is the tenant-correctness boundary, not the query access path").
CREATE INDEX ix_ioe_scenario_freshness
    ON ioe.scenario (user_id, freshness_status)
    WHERE visibility_status = 'active';
CREATE INDEX ix_ioe_scenario_superseded_by
    ON ioe.scenario (superseded_by_scenario_id)
    WHERE superseded_by_scenario_id IS NOT NULL;
CREATE INDEX ix_ioe_scenario_refreshed_from
    ON ioe.scenario (refreshed_from_scenario_id)
    WHERE refreshed_from_scenario_id IS NOT NULL;

-- ---- 2. The typed scenario specification ------------------------------------
-- Levers are referenced BY CODE with structured parameters. There is no column
-- for a field path, a JSON patch, an expression, or any other executable
-- mutation data — the shape of this table is itself the control.
CREATE TABLE ioe.scenario_lever (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    scenario_id     uuid NOT NULL REFERENCES ioe.scenario(id) ON DELETE CASCADE,
    apply_order     smallint NOT NULL,
    lever_code      text NOT NULL,          -- resolved ONLY by the pinned registry
    parameters      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scenario_id, apply_order),
    CONSTRAINT scenario_lever_code_is_a_code
        -- a code, never a path or an expression
        CHECK (lever_code ~ '^[A-Z][A-Z0-9_]{2,63}$'),
    CONSTRAINT scenario_lever_parameters_is_object
        CHECK (jsonb_typeof(parameters) = 'object')
);
CREATE INDEX ix_ioe_scenario_lever ON ioe.scenario_lever (scenario_id, apply_order);

-- Structured assumptions: a registered code plus a typed value. Never free text
-- that some later stage has to interpret.
CREATE TABLE ioe.scenario_assumption (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    scenario_id         uuid NOT NULL REFERENCES ioe.scenario(id) ON DELETE CASCADE,
    assumption_code     text NOT NULL,
    value_number        numeric(18,6),
    value_text          text,
    value_boolean       boolean,
    materiality         text NOT NULL DEFAULT 'medium'
                        CHECK (materiality IN ('low','medium','high')),
    -- vocabularies mirror the domain enums AssumptionSource / AssumptionCertainty
    source              text NOT NULL DEFAULT 'user'
                        CHECK (source IN ('user','platform','analysis')),
    certainty           text NOT NULL DEFAULT 'user_asserted'
                        CHECK (certainty IN ('user_asserted','platform_default',
                                             'derived_from_data','statutory_known')),
    affects_eligibility boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scenario_id, assumption_code),
    CONSTRAINT scenario_assumption_code_is_a_code
        CHECK (assumption_code ~ '^[A-Z][A-Z0-9_]{2,63}$'),
    -- exactly one typed value; never an untyped blob
    CONSTRAINT scenario_assumption_one_typed_value
        CHECK (num_nonnulls(value_number, value_text, value_boolean) = 1)
);
CREATE INDEX ix_ioe_scenario_assumption ON ioe.scenario_assumption (scenario_id);

-- ---- 3. Result: five support-score fields + auditable uncertainty -----------
ALTER TABLE ioe.scenario_result
    ADD COLUMN raw_support_score         numeric(5,2),
    ADD COLUMN assumption_adjusted_score numeric(5,2),
    ADD COLUMN display_support_score     numeric(5,2),
    ADD COLUMN support_cap_applied       boolean NOT NULL DEFAULT false,
    ADD COLUMN support_cap_reason_code   text,
    ADD COLUMN result_schema_version     text,
    ADD COLUMN objective_code            text,
    ADD COLUMN objective_version         text,
    ADD COLUMN objective_value_baseline  ref.money_amt,
    ADD COLUMN objective_value_scenario  ref.money_amt,
    ADD COLUMN objective_delta           ref.money_amt;

ALTER TABLE ioe.scenario_result
    ADD CONSTRAINT scenario_raw_support_range
        CHECK (raw_support_score IS NULL
               OR raw_support_score BETWEEN 0 AND 100) NOT VALID,
    ADD CONSTRAINT scenario_adjusted_support_range
        CHECK (assumption_adjusted_score IS NULL
               OR assumption_adjusted_score BETWEEN 0 AND 100) NOT VALID,
    ADD CONSTRAINT scenario_display_support_range
        CHECK (display_support_score IS NULL
               OR display_support_score BETWEEN 0 AND 100) NOT VALID,
    ADD CONSTRAINT scenario_display_not_above_adjusted
        CHECK (display_support_score IS NULL
               OR assumption_adjusted_score IS NULL
               OR display_support_score <= assumption_adjusted_score) NOT VALID,
    ADD CONSTRAINT scenario_cap_reason_iff_capped
        CHECK ((support_cap_applied AND support_cap_reason_code IS NOT NULL)
               OR (NOT support_cap_applied
                   AND support_cap_reason_code IS NULL)) NOT VALID,
    -- the delta is defined by the two objective values and can never drift
    ADD CONSTRAINT scenario_objective_delta_consistent
        CHECK (objective_delta IS NULL
               OR objective_value_baseline IS NULL
               OR objective_value_scenario IS NULL
               OR objective_delta = objective_value_baseline
                                    - objective_value_scenario) NOT VALID;

ALTER TABLE ioe.scenario_result VALIDATE CONSTRAINT scenario_raw_support_range;
ALTER TABLE ioe.scenario_result VALIDATE CONSTRAINT scenario_adjusted_support_range;
ALTER TABLE ioe.scenario_result VALIDATE CONSTRAINT scenario_display_support_range;
ALTER TABLE ioe.scenario_result VALIDATE CONSTRAINT scenario_display_not_above_adjusted;
ALTER TABLE ioe.scenario_result VALIDATE CONSTRAINT scenario_cap_reason_iff_capped;
ALTER TABLE ioe.scenario_result VALIDATE CONSTRAINT scenario_objective_delta_consistent;

-- confidence_score is DERIVED from display_support_score, exactly as on
-- optimization_candidate: trigger for the derivation, CHECK as an independent
-- row invariant that still holds if the derivation does not run.
CREATE TRIGGER trg_derive_confidence_score
    BEFORE INSERT OR UPDATE ON ioe.scenario_result
    FOR EACH ROW EXECUTE FUNCTION ioe.derive_confidence_score();

ALTER TABLE ioe.scenario_result
    ADD CONSTRAINT scenario_confidence_matches_display
        CHECK (display_support_score IS NULL
               OR confidence_score IS NULL
               OR confidence_score = round(display_support_score)::smallint) NOT VALID;
ALTER TABLE ioe.scenario_result VALIDATE CONSTRAINT scenario_confidence_matches_display;

COMMENT ON COLUMN ioe.scenario_result.confidence_score IS
    'DERIVED from display_support_score by trigger; never supplied by callers. A support score, not a probability.';

-- Per-factor uncertainty, so a support score can be audited rather than trusted.
CREATE TABLE ioe.scenario_confidence_component (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    scenario_id     uuid NOT NULL REFERENCES ioe.scenario(id) ON DELETE CASCADE,
    factor_code     text NOT NULL,
    value           numeric(9,6) NOT NULL,
    weight          numeric(9,6) NOT NULL,
    contribution    numeric(9,6) NOT NULL,
    reason_code     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scenario_id, factor_code)
);
CREATE INDEX ix_ioe_scenario_confidence ON ioe.scenario_confidence_component (scenario_id);

-- ---- 4. Immutability of the new scenario evidence ---------------------------
-- `scenario_result` and `scenario_input_change` are already covered by the
-- immutability loop in 21_ioe.sql; only the tables added here need triggers.
-- The pinned SPEC is evidence too: a lever or assumption that could be edited
-- after the fact would mean the stored spec hash no longer describes the spec.
--
-- Archiving changes visibility on the PARENT row and leaves every child
-- untouched, which is why "delete" can be an archive without losing anything.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'scenario_lever', 'scenario_assumption', 'scenario_confidence_component'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_immutable BEFORE UPDATE OR DELETE ON ioe.%I
             FOR EACH ROW EXECUTE FUNCTION ioe.reject_result_mutation();', t);
    END LOOP;
END $$;

-- 21_ioe.sql already seals the hashes on completion. The pinned specification
-- columns added above must be sealed on exactly the same terms, so the existing
-- trigger is REPLACED with a wider argument list rather than a second trigger
-- being added beside it (two triggers on one table would both fire and the
-- narrower one would report a misleading reason).
--
-- Label, note, visibility, freshness and supersession deliberately REMAIN
-- writable — those are precisely how archive and refresh work.
DROP TRIGGER IF EXISTS trg_guard_transition ON ioe.scenario;
CREATE TRIGGER trg_guard_transition
    BEFORE UPDATE ON ioe.scenario
    FOR EACH ROW EXECUTE FUNCTION ioe.guard_workflow_transition(
        'scenario_spec_hash', 'scenario_result_hash',
        'baseline_input_snapshot_hash', 'baseline_result_hash', 'baseline_tax',
        'rule_snapshot_id', 'lever_registry_version', 'manifest_hash',
        'version_manifest', 'objective_code', 'objective_version',
        'tax_year', 'jurisdiction', 'base_analysis_id', 'user_id', 'completed_at'
    );

-- ---- 5. Row-Level Security on the new child tables --------------------------
-- Ownership resolves through ioe.scenario.user_id, matching migration 0032.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'scenario_lever', 'scenario_assumption', 'scenario_confidence_component'
    ] LOOP
        EXECUTE format('ALTER TABLE ioe.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE ioe.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($f$
            CREATE POLICY p_self_%1$s ON ioe.%1$I
                USING (EXISTS (SELECT 1 FROM ioe.scenario s
                                WHERE s.id = scenario_id
                                  AND s.user_id = ref.current_app_user()))
                WITH CHECK (EXISTS (SELECT 1 FROM ioe.scenario s
                                     WHERE s.id = scenario_id
                                       AND s.user_id = ref.current_app_user()))
        $f$, t);
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    ioe.scenario_lever, ioe.scenario_assumption, ioe.scenario_confidence_component
    TO onyx_app_rw;
GRANT SELECT ON
    ioe.scenario_lever, ioe.scenario_assumption, ioe.scenario_confidence_component
    TO onyx_app_ro;
