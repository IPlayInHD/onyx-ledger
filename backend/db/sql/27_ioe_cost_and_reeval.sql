-- =============================================================================
-- Onyx Ledger — 27 · Cost-normalization provenance + re-evaluation state
-- Alembic revision: 0033_ioe_cost_and_reeval
-- IOE architecture Revision 2.1 §B, §C; P4 closing items 3 and 4.
--
-- Two additions, both about not losing information:
--
-- 1. COST NORMALIZATION PROVENANCE. The legacy `required_cash_contribution`
--    conflated two different economic facts — cash that must be available, and
--    value moved into an account the user still owns. P4 separates them, so a
--    legacy value has to be resolved to one of them. That resolution is a
--    DERIVATION, not authored data, so the authored value is kept verbatim
--    alongside the basis on which it was resolved. Anyone reading a stored cost
--    can see both what the rule said and why it was classified as it was.
--
-- 2. RE-EVALUATION STATE. When an earlier portfolio action changes the facts a
--    later candidate's eligibility depends on, the candidate is re-evaluated
--    against the SAME pinned rule-version set. Where that is not possible the
--    candidate is not silently kept and not silently dropped: it is marked
--    `requires_re_evaluation` so the unresolved state is visible.
--
-- ADDITIVE ONLY. Existing rows remain valid; both flags default to the
-- "nothing was derived / nothing is pending" state.
-- =============================================================================

-- ---- 1. Cost-normalization provenance ---------------------------------------
ALTER TABLE ioe.candidate_cost
    ADD COLUMN authored_cost_type text,
    ADD COLUMN cost_type_source   text NOT NULL DEFAULT 'authored_verbatim',
    ADD COLUMN taxonomy_version   text;

ALTER TABLE ioe.candidate_cost
    ADD CONSTRAINT candidate_cost_type_source_check
    CHECK (cost_type_source IN (
        -- the rule authored a P4 taxonomy value; copied verbatim
        'authored_verbatim',
        -- legacy value with exactly one P4 meaning (required_expenditure)
        'legacy_exact_synonym',
        -- legacy ambiguous value resolved by the PINNED lever registry
        'lever_registry',
        -- legacy ambiguous value with no registry entry: resolved to the
        -- conservative reading that preserves pre-P4 behaviour
        'legacy_conservative_default'
    ));

COMMENT ON COLUMN ioe.candidate_cost.authored_cost_type IS
    'The cost_type exactly as the rule authored it, before P4 normalization. NULL only for rows written before this column existed.';
COMMENT ON COLUMN ioe.candidate_cost.cost_type_source IS
    'How cost_type was arrived at. Anything other than authored_verbatim means the value is an IOE derivation from a legacy rule value, not rule data.';
COMMENT ON COLUMN ioe.candidate_cost.taxonomy_version IS
    'Version of the cost-taxonomy resolution rules that produced cost_type.';

-- ---- 2. Re-evaluation state --------------------------------------------------
ALTER TABLE ioe.optimization_candidate
    ADD COLUMN requires_re_evaluation    boolean NOT NULL DEFAULT false,
    ADD COLUMN re_evaluation_reason_code text;

COMMENT ON COLUMN ioe.optimization_candidate.requires_re_evaluation IS
    'True when an earlier portfolio action changed the facts this candidate''s eligibility depends on and re-evaluation against the pinned rule snapshot could not resolve it. The candidate is excluded, never acted on.';

-- A candidate whose eligibility could not be re-resolved is excluded under its
-- own membership value rather than being folded into a generic constraint
-- exclusion, so "we could not tell" never reads as "we checked and it failed".
DO $$
DECLARE c text;
BEGIN
    FOR c IN
        SELECT con.conname FROM pg_constraint con
         WHERE con.conrelid = 'ioe.optimization_candidate'::regclass
           AND con.contype = 'c'
           AND con.conkey = ARRAY[(SELECT a.attnum FROM pg_attribute a
                                    WHERE a.attrelid = con.conrelid
                                      AND a.attname = 'portfolio_membership')]
    LOOP
        EXECUTE format(
            'ALTER TABLE ioe.optimization_candidate DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE ioe.optimization_candidate
    ADD CONSTRAINT optimization_candidate_portfolio_membership_check
    CHECK (portfolio_membership IN (
        'selected', 'excluded_conflict', 'excluded_constraint',
        'excluded_not_evaluable', 'deferred_timing',
        'deferred_pending_combination',
        -- P4 closing item 4
        'requires_re_evaluation'
    )) NOT VALID;
ALTER TABLE ioe.optimization_candidate
    VALIDATE CONSTRAINT optimization_candidate_portfolio_membership_check;

-- The exclusion record mirrors the same vocabulary.
ALTER TABLE ioe.portfolio_exclusion
    ADD CONSTRAINT portfolio_exclusion_membership_check
    CHECK (membership IN (
        'excluded_conflict', 'excluded_constraint', 'excluded_not_evaluable',
        'deferred_timing', 'deferred_pending_combination',
        'requires_re_evaluation'
    )) NOT VALID;
ALTER TABLE ioe.portfolio_exclusion
    VALIDATE CONSTRAINT portfolio_exclusion_membership_check;
