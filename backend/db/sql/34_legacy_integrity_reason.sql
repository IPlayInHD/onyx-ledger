-- =============================================================================
-- Onyx Ledger — 34 · Legacy replay-integrity reason  (schema: ioe)
-- Alembic revision: 0040_legacy_integrity_reason
--
-- Item 3B closeout. The verifier had exactly two ways to describe a result it
-- could not confirm: `mismatch` (the replay ran and produced a different
-- identity) and `unavailable` (a pinned dependency could not be loaded). A
-- result sealed BEFORE the frozen-input correction fits neither.
--
-- Replaying such a result compares its sealed numbers against a snapshot it was
-- never computed from. The difference that produces is not a deterministic
-- replay regression — the guarantee did not exist yet — so recording it as a
-- `mismatch` would assert a fault nothing has demonstrated, and would bury
-- genuine regressions inside a population of old rows that can only grow.
--
-- `LEGACY_EXECUTION_POLICY_UNVERIFIABLE` is therefore an `unavailable` reason:
-- the dependency that would make verification possible — a recorded frozen
-- input identity — was never written. It is deliberately its own code so that
-- dashboards and readiness reporting can exclude age from fault counts.
--
-- ADDITIVE in effect. One CHECK constraint is widened to admit one further
-- enumerated value; no existing value, row, column, index or policy changes,
-- and every row that satisfied the old constraint satisfies the new one.
-- =============================================================================

ALTER TABLE ioe.integrity_check
    DROP CONSTRAINT IF EXISTS integrity_check_reason_code_check;

ALTER TABLE ioe.integrity_check
    ADD CONSTRAINT integrity_check_reason_code_check CHECK (reason_code IN (
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
        'LEGACY_EXECUTION_POLICY_UNVERIFIABLE',
        'REPLAY_EXECUTION_FAILED',
        'SEALED_EVIDENCE_INCOMPLETE'));

-- The mismatch constraint is restated unchanged, as documentation of intent:
-- the legacy reason must NEVER accompany a mismatch. It is listed here so a
-- future edit that adds it to the mismatch set has to delete this comment.
--   CONSTRAINT ck_integrity_check_mismatch_reason CHECK (
--       status <> 'mismatch'
--    OR reason_code IN ('RESULT_HASH_MISMATCH','PORTFOLIO_HASH_MISMATCH'))

COMMENT ON COLUMN ioe.integrity_check.reason_code IS
    'Closed enumeration explaining the outcome. LEGACY_EXECUTION_POLICY_UNVERIFIABLE marks a result sealed before the frozen-input correction: it is an unavailable reason, never a mismatch, because the result was computed from sources that were never recorded and so nothing was compared.';

-- Counting aid for readiness reporting: legacy rows must be excludable from
-- genuine replay-failure counts without a sequential scan over history.
CREATE INDEX IF NOT EXISTS ix_ioe_integrity_check_reason
    ON ioe.integrity_check (reason_code, completed_at DESC);
