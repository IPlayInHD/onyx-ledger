-- =============================================================================
-- Onyx Ledger — 33 · Input execution policy  (schema: ioe)
-- Alembic revision: 0039_input_execution_policy
--
-- Item 3A. The orchestrator pinned a frozen analysis snapshot and put its hash
-- into `optimization_spec_hash`, then rebuilt its tax inputs from the user's
-- LIVE financial tables. Between those two moments the data can change, so a
-- run could be sealed under snapshot A's identity while being calculated from
-- values B. The spec hash was therefore not an identity for the calculation it
-- labelled.
--
-- The correction makes computation read exclusively from the pinned snapshot.
-- This column records WHICH rule a given run was executed under, so a reader
-- never has to infer it from a deployment date:
--
--   live_source_legacy   computed before the correction; inputs may not match
--                        the snapshot the spec hash names
--   frozen_snapshot_v1   computed exclusively from the pinned snapshot
--
-- Historical rows default to `live_source_legacy`. They are NOT backfilled as
-- corrected: claiming a guarantee those runs never had would be worse than the
-- original defect, because it would be undetectable.
--
-- ADDITIVE. No existing column, constraint or policy is altered.
-- =============================================================================

ALTER TABLE ioe.optimization_run
    ADD COLUMN IF NOT EXISTS input_execution_policy_version text NOT NULL
        DEFAULT 'live_source_legacy';

ALTER TABLE ioe.optimization_run
    DROP CONSTRAINT IF EXISTS ck_optimization_run_input_execution_policy;
ALTER TABLE ioe.optimization_run
    ADD CONSTRAINT ck_optimization_run_input_execution_policy
    CHECK (input_execution_policy_version IN
        ('live_source_legacy', 'frozen_snapshot_v1'));

COMMENT ON COLUMN ioe.optimization_run.input_execution_policy_version IS
    'Which input-execution rule this run was computed under. live_source_legacy runs predate item 3A and may have been calculated from data that differs from the snapshot their spec hash names; frozen_snapshot_v1 runs were calculated exclusively from the pinned snapshot. Historical rows are never backfilled as frozen_snapshot_v1.';

-- Operators need to find the affected population without scanning evidence.
CREATE INDEX IF NOT EXISTS ix_ioe_optimization_run_execution_policy
    ON ioe.optimization_run (input_execution_policy_version, created_at DESC);
