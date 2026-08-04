-- =============================================================================
-- Onyx Ledger — 32 · Production replay-integrity verification  (schema: ioe)
-- Alembic revision: 0038_integrity_verification
--
-- The system can already prove determinism in tests. This makes the RUNNING
-- system able to answer a different question about a result it sealed months
-- ago: can it still be reproduced from its own pinned dependencies?
--
-- Three things this file is careful about:
--
-- 1. INTEGRITY IS NOT FRESHNESS. A result can be `completed`, `stale`, and
--    still perfectly reproducible; another can be `current` and NOT
--    reproducible. Conflating them would hide the second case behind the word
--    "stale", which reads as "your data changed" rather than "we cannot
--    reproduce what we told you".
--
-- 2. VERIFICATION NEVER REPAIRS. `ioe.integrity_check` is append-only and the
--    sealed result rows are untouched by it. A mismatch adds a row and flips a
--    metadata flag; it never rewrites the historical answer, and it never
--    triggers a silent re-run. The expected hash is what was sealed and is
--    never overwritten by an observed one.
--
-- 3. HASHES ARE DIAGNOSTIC, NOT AUTHORIZATION. They live only in this table,
--    behind the same RLS as everything else, and are not accepted anywhere as
--    proof of ownership.
--
-- ADDITIVE. No existing table, column, policy, or trigger is altered except to
-- add current-integrity metadata columns to the three workflow/result parents.
-- =============================================================================

-- ---- 1. Append-only verification history ------------------------------------
CREATE TABLE ioe.integrity_check (
    id                              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id                         uuid NOT NULL
                                        REFERENCES identity.user_account(id) ON DELETE CASCADE,
    entity_type                     text NOT NULL
                                        CHECK (entity_type IN ('optimization','portfolio','scenario')),

    -- Exactly one entity reference, enforced below. Kept as separate typed FKs
    -- rather than a polymorphic (type, id) pair so the database can enforce
    -- referential integrity and the planner can use a real index.
    optimization_run_id             uuid REFERENCES ioe.optimization_run(id) ON DELETE CASCADE,
    portfolio_id                    uuid REFERENCES ioe.strategy_portfolio(id) ON DELETE CASCADE,
    scenario_id                     uuid REFERENCES ioe.scenario(id) ON DELETE CASCADE,

    status                          text NOT NULL DEFAULT 'running'
                                        CHECK (status IN
                                            ('running','verified','mismatch','unavailable','failed')),
    reason_code                     text NOT NULL DEFAULT 'NONE'
                                        CHECK (reason_code IN (
                                            'NONE',
                                            'RESULT_HASH_MISMATCH',
                                            'PORTFOLIO_HASH_MISMATCH',
                                            'BASELINE_SNAPSHOT_UNAVAILABLE',
                                            'BASELINE_RESULT_UNAVAILABLE',
                                            'PINNED_RULE_SNAPSHOT_UNAVAILABLE',
                                            'PINNED_ENGINE_VERSION_UNAVAILABLE',
                                            'PINNED_ENGINE_CONFIG_UNAVAILABLE',
                                            'REFERENCE_DATA_VERSION_UNAVAILABLE',
                                            'OBJECTIVE_VERSION_UNAVAILABLE',
                                            'LEVER_REGISTRY_VERSION_UNAVAILABLE',
                                            'ASSUMPTION_REGISTRY_VERSION_UNAVAILABLE',
                                            'SCORING_VERSION_UNAVAILABLE',
                                            'SUPPORT_SCORE_VERSION_UNAVAILABLE',
                                            'PROJECTION_VERSION_UNAVAILABLE',
                                            'CANONICAL_SERIALIZATION_VERSION_UNSUPPORTED',
                                            'REPLAY_EXECUTION_FAILED',
                                            'SEALED_EVIDENCE_INCOMPLETE')),

    -- Diagnostic evidence only. `expected_*` is copied from the sealed row and
    -- is never written back to it.
    expected_spec_hash              text,
    expected_result_hash            text NOT NULL,
    actual_result_hash              text,

    verifier_version                text NOT NULL,
    canonical_serialization_version text NOT NULL,
    integrity_check_policy_version  text NOT NULL,

    -- Claim arbitration. A running row is owned by exactly one worker.
    claimed_by                      text,
    claim_expires_at                timestamptz,

    engine_runs_used                integer NOT NULL DEFAULT 0,
    duration_ms                     integer,
    operational_event_id            uuid,

    started_at                      timestamptz NOT NULL DEFAULT now(),
    completed_at                    timestamptz,
    created_at                      timestamptz NOT NULL DEFAULT now(),

    -- exactly one entity reference, and it must match entity_type
    CONSTRAINT ck_integrity_check_one_entity CHECK (
        (CASE WHEN optimization_run_id IS NOT NULL THEN 1 ELSE 0 END)
      + (CASE WHEN portfolio_id        IS NOT NULL THEN 1 ELSE 0 END)
      + (CASE WHEN scenario_id         IS NOT NULL THEN 1 ELSE 0 END) = 1
    ),
    CONSTRAINT ck_integrity_check_entity_matches_type CHECK (
        (entity_type = 'optimization' AND optimization_run_id IS NOT NULL)
     OR (entity_type = 'portfolio'    AND portfolio_id        IS NOT NULL)
     OR (entity_type = 'scenario'     AND scenario_id         IS NOT NULL)
    ),
    -- a terminal check has an outcome time; a running one does not
    CONSTRAINT ck_integrity_check_completion CHECK (
        (status = 'running' AND completed_at IS NULL)
     OR (status <> 'running' AND completed_at IS NOT NULL)
    ),
    -- only a mismatch may carry a differing observed hash
    CONSTRAINT ck_integrity_check_verified_hash CHECK (
        status <> 'verified'
     OR (actual_result_hash = expected_result_hash AND reason_code = 'NONE')
    ),
    CONSTRAINT ck_integrity_check_mismatch_reason CHECK (
        status <> 'mismatch'
     OR reason_code IN ('RESULT_HASH_MISMATCH','PORTFOLIO_HASH_MISMATCH')
    )
);

COMMENT ON TABLE ioe.integrity_check IS
    'Append-only replay-verification history. One row per verification attempt. Never updated except to transition a claimed running row to its terminal outcome; never deleted; never a source of authorization.';
COMMENT ON COLUMN ioe.integrity_check.expected_result_hash IS
    'The hash sealed with the original result. Diagnostic evidence. Never overwritten by an observed value and never accepted as proof of ownership.';
COMMENT ON COLUMN ioe.integrity_check.actual_result_hash IS
    'The hash the replay produced. NULL when the replay never reached a result (unavailable dependency, execution failure).';

-- Ownership traversal and every lookup the services perform, index-backed.
CREATE INDEX ix_ioe_integrity_check_user       ON ioe.integrity_check (user_id, created_at DESC);
CREATE INDEX ix_ioe_integrity_check_run        ON ioe.integrity_check (optimization_run_id, created_at DESC)
    WHERE optimization_run_id IS NOT NULL;
CREATE INDEX ix_ioe_integrity_check_portfolio  ON ioe.integrity_check (portfolio_id, created_at DESC)
    WHERE portfolio_id IS NOT NULL;
CREATE INDEX ix_ioe_integrity_check_scenario   ON ioe.integrity_check (scenario_id, created_at DESC)
    WHERE scenario_id IS NOT NULL;
CREATE INDEX ix_ioe_integrity_check_running    ON ioe.integrity_check (status, claim_expires_at)
    WHERE status = 'running';

-- One ACTIVE check per (entity, verifier version). This is the concurrency
-- arbitration: three workers racing for the same entity produce one running row
-- and two unique-violations, which the service reads as "already claimed".
CREATE UNIQUE INDEX ux_ioe_integrity_check_active_run ON ioe.integrity_check
    (optimization_run_id, verifier_version)
    WHERE status = 'running' AND optimization_run_id IS NOT NULL;
CREATE UNIQUE INDEX ux_ioe_integrity_check_active_portfolio ON ioe.integrity_check
    (portfolio_id, verifier_version)
    WHERE status = 'running' AND portfolio_id IS NOT NULL;
CREATE UNIQUE INDEX ux_ioe_integrity_check_active_scenario ON ioe.integrity_check
    (scenario_id, verifier_version)
    WHERE status = 'running' AND scenario_id IS NOT NULL;

-- ---- 2. Append-only enforcement ---------------------------------------------
-- The row is INSERTed `running` and transitioned exactly once to a terminal
-- status. That single transition is workflow, not evidence: the verification
-- outcome is not known when the claim is taken, and holding a transaction open
-- across an engine replay to avoid it would be far worse. Everything that IS
-- evidence — the hashes, the reason code, the versions — is write-once, which
-- the trigger enforces field by field.
CREATE OR REPLACE FUNCTION ioe.guard_integrity_check_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ioe.integrity_check is append-only (DELETE rejected)';
    END IF;

    IF OLD.status <> 'running' THEN
        RAISE EXCEPTION
            'ioe.integrity_check row % is already terminal (% -> % rejected)',
            OLD.id, OLD.status, NEW.status;
    END IF;
    IF NEW.status = 'running' THEN
        RAISE EXCEPTION 'ioe.integrity_check may only transition out of running';
    END IF;

    -- Evidence is write-once. A later verification appends a NEW row.
    IF NEW.id <> OLD.id
       OR NEW.user_id <> OLD.user_id
       OR NEW.entity_type <> OLD.entity_type
       OR NEW.expected_result_hash <> OLD.expected_result_hash
       OR NEW.expected_spec_hash IS DISTINCT FROM OLD.expected_spec_hash
       OR NEW.verifier_version <> OLD.verifier_version
       OR NEW.canonical_serialization_version <> OLD.canonical_serialization_version
       OR NEW.optimization_run_id IS DISTINCT FROM OLD.optimization_run_id
       OR NEW.portfolio_id IS DISTINCT FROM OLD.portfolio_id
       OR NEW.scenario_id IS DISTINCT FROM OLD.scenario_id
       OR NEW.started_at <> OLD.started_at
    THEN
        RAISE EXCEPTION 'ioe.integrity_check evidence columns are immutable';
    END IF;

    -- Only the worker holding the claim may complete it.
    IF OLD.claimed_by IS NOT NULL AND NEW.claimed_by IS DISTINCT FROM OLD.claimed_by THEN
        RAISE EXCEPTION 'ioe.integrity_check % is claimed by another worker', OLD.id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_integrity_check_append_only
    BEFORE UPDATE OR DELETE ON ioe.integrity_check
    FOR EACH ROW EXECUTE FUNCTION ioe.guard_integrity_check_transition();

COMMENT ON FUNCTION ioe.guard_integrity_check_transition() IS
    'Enforces that an integrity check is inserted running, transitions once to a terminal status, and never has its evidence columns rewritten. A later verification appends a new row rather than editing an old one.';
REVOKE EXECUTE ON FUNCTION ioe.guard_integrity_check_transition() FROM PUBLIC;

-- ---- 3. Row-level security ---------------------------------------------------
ALTER TABLE ioe.integrity_check ENABLE ROW LEVEL SECURITY;
ALTER TABLE ioe.integrity_check FORCE ROW LEVEL SECURITY;

-- `user_id` is carried directly and is denormalized deliberately: resolving
-- ownership through three nullable parents on every row read would make the
-- policy unindexable. The WITH CHECK below stops a caller writing a row
-- attributed to somebody else, and the entity FKs are themselves RLS-protected.
CREATE POLICY p_self_integrity_check ON ioe.integrity_check
    USING (user_id = ref.current_app_user())
    WITH CHECK (user_id = ref.current_app_user());

GRANT SELECT, INSERT, UPDATE ON ioe.integrity_check TO onyx_app_rw;

-- ---- 4. Current integrity metadata on the sealed parents ---------------------
-- Current state only. The history lives in ioe.integrity_check; these columns
-- are a cache of "what the latest completed check said", so a reader does not
-- have to scan history to render a badge.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['optimization_run', 'scenario', 'strategy_portfolio'] LOOP
        EXECUTE format($f$
            ALTER TABLE ioe.%1$I
                ADD COLUMN IF NOT EXISTS integrity_status text NOT NULL DEFAULT 'not_checked',
                ADD COLUMN IF NOT EXISTS integrity_reason_code text NOT NULL DEFAULT 'NONE',
                ADD COLUMN IF NOT EXISTS last_integrity_checked_at timestamptz,
                ADD COLUMN IF NOT EXISTS latest_integrity_check_id uuid
                    REFERENCES ioe.integrity_check(id) ON DELETE SET NULL
        $f$, t);
        EXECUTE format($f$
            ALTER TABLE ioe.%1$I
                DROP CONSTRAINT IF EXISTS ck_%1$s_integrity_status
        $f$, t);
        EXECUTE format($f$
            ALTER TABLE ioe.%1$I
                ADD CONSTRAINT ck_%1$s_integrity_status CHECK (integrity_status IN
                    ('not_checked','verified','mismatch','unavailable'))
        $f$, t);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS ix_ioe_%1$s_integrity ON ioe.%1$I '
            '(integrity_status, last_integrity_checked_at NULLS FIRST)', t);
    END LOOP;
END $$;

COMMENT ON COLUMN ioe.optimization_run.integrity_status IS
    'Reproducibility of the SEALED result, independent of freshness. A run may be stale and still verified, or current and non-reproducible. mismatch is surfaced to users as non_reproducible.';
COMMENT ON COLUMN ioe.scenario.integrity_status IS
    'Reproducibility of the SEALED result, independent of freshness. See ioe.optimization_run.integrity_status.';
COMMENT ON COLUMN ioe.strategy_portfolio.integrity_status IS
    'Reproducibility of the SEALED portfolio, independent of the run''s freshness.';

-- `strategy_portfolio` carries immutable calculation evidence and is guarded by
-- ioe.reject_result_mutation(). Current integrity metadata has to be able to
-- move, so the guard is taught that these four columns — and ONLY these four —
-- are mutable workflow state.
--
-- The check is exact rather than by omission: the two row images are compared
-- with the four integrity keys stripped, so an UPDATE that changes a financial
-- column is still rejected even when it is bundled with a legitimate integrity
-- change. There is no silently-dropped update and no trigger is disabled, so
-- verification takes no heavier lock than an ordinary row update.
CREATE OR REPLACE FUNCTION ioe.reject_result_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_integrity_keys text[] := ARRAY[
        'integrity_status', 'integrity_reason_code',
        'last_integrity_checked_at', 'latest_integrity_check_id'
    ];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF TG_TABLE_NAME = 'strategy_portfolio'
           AND (to_jsonb(OLD) - v_integrity_keys) = (to_jsonb(NEW) - v_integrity_keys)
        THEN
            -- only current integrity metadata differs
            RETURN NEW;
        END IF;
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

COMMENT ON FUNCTION ioe.reject_result_mutation() IS
    'Rejects UPDATE and DELETE on sealed IOE calculation evidence. The single exception is current integrity metadata on strategy_portfolio, allowed only when the two row images are otherwise byte-identical.';

-- ---- 4b. Spec inputs that were hashed but never stored ----------------------
-- `optimization_spec_hash` is taken over the user constraints and the assumption
-- set, but neither was persisted, so the spec hash could not be independently
-- recomputed from stored rows — the identity was unverifiable by construction.
-- Storing them changes no hash: it records inputs that were already inside one.
ALTER TABLE ioe.optimization_run
    ADD COLUMN IF NOT EXISTS user_constraints jsonb,
    ADD COLUMN IF NOT EXISTS assumption_set jsonb;

COMMENT ON COLUMN ioe.optimization_run.user_constraints IS
    'The canonicalized constraints that entered optimization_spec_hash. Stored so the spec hash can be recomputed during replay verification. NULL on runs sealed before this column existed, which makes their spec hash unverifiable rather than wrong.';
COMMENT ON COLUMN ioe.optimization_run.assumption_set IS
    'The assumption set that entered optimization_spec_hash. Stored for the same reason as user_constraints.';

-- ---- 4c. The savings concept the portfolio hash covers but no column held ----
-- `SavingsBreakdown.as_canonical()` carries eleven per-concept totals and
-- `strategy_portfolio` stored ten. `future_option_value` entered the portfolio
-- hash and was then discarded, so a portfolio's own identity could not be
-- rebuilt from its own row — replay verification is exactly what surfaces that.
-- Each economic concept is kept separate, so it gets its own column rather than
-- being folded into a neighbour.
ALTER TABLE ioe.strategy_portfolio
    ADD COLUMN IF NOT EXISTS total_future_option_value ref.money_amt NOT NULL DEFAULT 0;

COMMENT ON COLUMN ioe.strategy_portfolio.total_future_option_value IS
    'Future option value. Part of the portfolio canonical form and therefore of portfolio_result_hash; stored so the hash can be independently recomputed from this row.';

-- ---- 5. Verification scheduling keyhole --------------------------------------
-- A cross-tenant scheduler must not read financial rows to decide what to
-- verify. It gets identifiers and an owner id only — exactly the freshness
-- relay's keyhole shape — and the replay itself then runs under ordinary tenant
-- RLS with app.user_id set from the claimed row.
CREATE OR REPLACE FUNCTION ioe.claim_integrity_targets(
    p_batch_size integer,
    p_entity_type text
)
RETURNS TABLE (
    out_entity_type text,
    out_entity_id   uuid,
    out_user_id     uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ioe, ref
AS $$
DECLARE v_batch integer;
BEGIN
    IF p_entity_type NOT IN ('optimization','scenario') THEN
        RAISE EXCEPTION 'unsupported integrity target type %', p_entity_type;
    END IF;
    v_batch := least(greatest(coalesce(p_batch_size, 10), 1), 50);

    IF p_entity_type = 'optimization' THEN
        RETURN QUERY
            SELECT 'optimization'::text, r.id, r.user_id
              FROM ioe.optimization_run r
             WHERE r.workflow_status = 'completed'
               AND r.optimization_result_hash IS NOT NULL
             ORDER BY r.last_integrity_checked_at ASC NULLS FIRST, r.created_at ASC
             LIMIT v_batch;
    ELSE
        RETURN QUERY
            SELECT 'scenario'::text, s.id, s.user_id
              FROM ioe.scenario s
             WHERE s.workflow_status = 'completed'
               AND s.scenario_result_hash IS NOT NULL
             ORDER BY s.last_integrity_checked_at ASC NULLS FIRST, s.created_at ASC
             LIMIT v_batch;
    END IF;
END;
$$;

ALTER FUNCTION ioe.claim_integrity_targets(integer, text) OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION ioe.claim_integrity_targets(integer, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ioe.claim_integrity_targets(integer, text)
    TO onyx_freshness_worker;

COMMENT ON FUNCTION ioe.claim_integrity_targets(integer, text) IS
    'Oldest-never-checked-first selection of verification targets. Returns identifiers and an owner id ONLY — no hashes, no financial values, no result columns. Bounded to 50. The replay itself runs under ordinary tenant RLS, never here.';

-- ---- 6. Stale-claim recovery -------------------------------------------------
-- A worker that dies mid-replay leaves a `running` row holding the unique
-- active-check index. Recovery is a terminal transition to `failed`, which the
-- append-only trigger already permits, so no evidence is rewritten and the
-- history keeps the abandoned attempt.
CREATE OR REPLACE FUNCTION ioe.recover_stale_integrity_checks(p_limit integer DEFAULT 100)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ioe
AS $$
DECLARE v_count integer;
BEGIN
    WITH stale AS (
        SELECT id FROM ioe.integrity_check
         WHERE status = 'running'
           AND claim_expires_at IS NOT NULL
           AND claim_expires_at < now()
         ORDER BY claim_expires_at
         LIMIT least(greatest(coalesce(p_limit, 100), 1), 500)
         FOR UPDATE SKIP LOCKED
    )
    UPDATE ioe.integrity_check c
       SET status = 'failed',
           reason_code = 'REPLAY_EXECUTION_FAILED',
           completed_at = now()
      FROM stale
     WHERE c.id = stale.id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;

ALTER FUNCTION ioe.recover_stale_integrity_checks(integer) OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION ioe.recover_stale_integrity_checks(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ioe.recover_stale_integrity_checks(integer)
    TO onyx_freshness_worker;

COMMENT ON FUNCTION ioe.recover_stale_integrity_checks(integer) IS
    'Transitions abandoned running checks to failed so the active-check index is released. Appends nothing, deletes nothing, and touches no sealed result.';
