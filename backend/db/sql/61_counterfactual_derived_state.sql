-- =============================================================================
-- Entry 12B1 — sealed counterfactual derived state (migration 0067)
-- =============================================================================
--
-- WHAT WAS MISSING. A sealed scenario recorded what the counterfactual tax
-- TOTAL was and nothing about what the counterfactual tax state CONTAINED. So a
-- baseline-vs-counterfactual comparison had a baseline made of line items,
-- opportunities, requirements and deadlines, and a counterfactual made of one
-- number — and differencing those would report every baseline detail as REMOVED
-- when it had merely never been persisted.
--
-- WHY COLUMNS AND NOT A TABLE. Measured, not assumed. `pg_column_size` on the
-- real canonical payload:
--
--     candidates   canonical bytes   JSONB stored   ratio
--              1             1,590          1,808   107%   (under TOAST)
--             25            26,027          2,225   8.1%
--            500           436,640         21,378   4.7%
--
-- TOAST compression is very effective on this payload because its keys repeat,
-- so five hundred candidates cost twenty-one kilobytes. The read path consumes
-- the artifact whole — replay and integrity hash it as a unit and never query
-- inside it — so per-candidate rows would buy queryability nobody needs and pay
-- for it with a 71st privacy-bearing table.
--
-- WHAT THE COLUMNS INHERIT, verified against pg_trigger rather than read off a
-- migration:
--
--     trg_immutable    reject_result_mutation
--                      UPDATE always refused; DELETE only under the certified
--                      evidence-purge GUC. Row-level, so it covers columns
--                      added later without being told about them.
--     trg_write_cutoff reject_scenario_detail_after_deletion_request
--                      a late TX-2 INSERT after a deletion request is already
--                      refused, and this payload arrives in that same INSERT.
--
-- That inheritance is the whole argument: no new table, no new trigger, no new
-- RLS policy, and no new privacy surface — the same row, carrying more of what
-- the scenario already computed.
--
-- LEGACY SCENARIOS ARE NOT TOUCHED. Historical rows keep NULL in both columns
-- and keep verifying under their own result-schema version. Backfilling them by
-- evaluating today's rules would fabricate historical evidence, which is why the
-- CHECK below permits "neither" and forbids only "exactly one".

ALTER TABLE ioe.scenario_result
    ADD COLUMN IF NOT EXISTS counterfactual_derived_state jsonb,
    ADD COLUMN IF NOT EXISTS counterfactual_derived_state_hash text;

COMMENT ON COLUMN ioe.scenario_result.counterfactual_derived_state IS
    'The sealed counterfactual derived state: the TaxEngineService line items '
    'and the pinned-rule candidate set this scenario evaluated. Canonicalized '
    'before hashing. NULL on scenarios sealed before Entry 12B1, which are '
    'never backfilled.';

COMMENT ON COLUMN ioe.scenario_result.counterfactual_derived_state_hash IS
    'domain_hash(DOMAIN_COUNTERFACTUAL_DERIVED_STATE, payload). Bound into '
    'scenario_result_hash for result schema v2 and later, so mutating the '
    'payload invalidates verification.';

-- Both or neither. A payload without its hash cannot be verified, and a hash
-- without its payload cannot be recomputed — either half alone is an artifact
-- that claims more integrity than it has. "Neither" stays legal because that is
-- exactly what every pre-12B1 scenario looks like.
ALTER TABLE ioe.scenario_result
    DROP CONSTRAINT IF EXISTS ck_counterfactual_derived_state_paired;
ALTER TABLE ioe.scenario_result
    ADD CONSTRAINT ck_counterfactual_derived_state_paired CHECK (
        (counterfactual_derived_state IS NULL
         AND counterfactual_derived_state_hash IS NULL)
        OR
        (counterfactual_derived_state IS NOT NULL
         AND counterfactual_derived_state_hash IS NOT NULL)
    );
