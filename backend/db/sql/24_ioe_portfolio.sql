-- =============================================================================
-- Onyx Ledger — 24 · IOE portfolio assembly and evaluation  (schema: ioe)
-- Alembic revision: 0030_ioe_portfolio
--
-- P4. Three changes, all additive:
--
--  1. COMMITMENT TAXONOMY. P1 modelled three cost types. P4 keeps each economic
--     concept distinct, because they behave differently and conflating them
--     misstates a user's position:
--       liquidity_commitment       cash that must be available and is tied up
--       asset_transfer             value moved into an account and RETAINED
--                                  (an RRSP contribution is not a cost)
--       nonrecoverable_expenditure money that is gone (a donation, an expense)
--       implementation_cost        fees, professional advice
--     The legacy values are retained so existing rows stay valid.
--
--  2. PORTFOLIO OBJECTIVE + OUTCOME TOTALS. The objective actually used is
--     pinned by CODE and VERSION, and its baseline value, final value, and delta
--     are all persisted, so a stored portfolio can be re-derived and audited
--     rather than taken on trust. Each economic outcome gets its own total; none
--     is summed into another.
--
--  3. EVALUATION TRACE. Every engine run performed during assembly is recorded
--     — its stage, the candidate it concerned, and the resulting objective value
--     — so the assembly is explainable step by step. The trace stores objective
--     values and codes only; it deliberately does NOT duplicate the user's
--     financial inputs, which already live in the frozen analysis snapshot.
--
-- The portfolio is a FEASIBLE, DETERMINISTIC, ENGINE-EVALUATED strategy set. It
-- is not a globally optimal portfolio, and `optimality_claim` has no value that
-- would let it claim to be.
-- =============================================================================

-- ---- 1. Commitment taxonomy -------------------------------------------------
DO $$
DECLARE c text;
BEGIN
    FOR c IN
        SELECT con.conname FROM pg_constraint con
         WHERE con.conrelid = 'ioe.candidate_cost'::regclass
           AND con.contype = 'c'
           AND con.conkey = ARRAY[(SELECT a.attnum FROM pg_attribute a
                                    WHERE a.attrelid = con.conrelid
                                      AND a.attname = 'cost_type')]
    LOOP
        EXECUTE format('ALTER TABLE ioe.candidate_cost DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE ioe.candidate_cost
    ADD CONSTRAINT candidate_cost_type_check
    CHECK (cost_type IN (
        -- legacy (retained; required_cash_contribution ≡ liquidity_commitment
        -- + asset_transfer before they were separated)
        'required_cash_contribution','required_expenditure','implementation_cost',
        -- P4 taxonomy
        'liquidity_commitment','asset_transfer','nonrecoverable_expenditure'
    )) NOT VALID;
ALTER TABLE ioe.candidate_cost VALIDATE CONSTRAINT candidate_cost_type_check;

-- ---- 2. Portfolio objective + per-concept totals -----------------------------
ALTER TABLE ioe.strategy_portfolio
    ADD COLUMN portfolio_objective_code    text,
    ADD COLUMN portfolio_objective_version text,
    ADD COLUMN objective_delta             ref.money_amt,
    ADD COLUMN search_budget_exhausted      boolean NOT NULL DEFAULT false,
    -- each economic outcome kept separate; never folded into another
    ADD COLUMN total_tax_reduction          ref.money_amt,
    ADD COLUMN total_refund_impact          ref.money_amt,
    ADD COLUMN total_refundable_benefit     ref.money_amt,
    ADD COLUMN total_liquidity_commitment   ref.money_amt,
    ADD COLUMN total_asset_transfer         ref.money_amt,
    ADD COLUMN total_nonrecoverable_expenditure ref.money_amt;

-- The delta is defined by the two objective values, so it can never drift from
-- them: baseline − final, at money scale.
ALTER TABLE ioe.strategy_portfolio
    ADD CONSTRAINT portfolio_objective_delta_consistent
    CHECK (objective_delta IS NULL
           OR objective_value_baseline IS NULL
           OR objective_value_final IS NULL
           OR objective_delta = objective_value_baseline - objective_value_final) NOT VALID;
ALTER TABLE ioe.strategy_portfolio
    VALIDATE CONSTRAINT portfolio_objective_delta_consistent;

COMMENT ON COLUMN ioe.strategy_portfolio.objective_delta IS
    'baseline objective value minus final objective value; sign convention: positive is an improvement.';
COMMENT ON COLUMN ioe.strategy_portfolio.search_budget_exhausted IS
    'True when assembly stopped because the bounded search budget ran out rather than because it had considered every candidate.';

-- ---- 3. Evaluation trace ----------------------------------------------------
-- One row per engine run performed during assembly. Immutable evidence.
CREATE TABLE ioe.portfolio_evaluation_step (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    portfolio_id        uuid NOT NULL REFERENCES ioe.strategy_portfolio(id) ON DELETE CASCADE,
    step_index          integer NOT NULL,
    stage               text NOT NULL CHECK (stage IN
                            ('baseline','standalone','incremental_trial','accepted',
                             'deferred_retry','final_combined','eligibility_recheck')),
    candidate_id        uuid REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    apply_order         smallint,
    objective_value     ref.money_amt,
    objective_delta     ref.money_amt,
    accepted            boolean,
    reason_code         text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (portfolio_id, step_index)
);
CREATE INDEX ix_portfolio_step ON ioe.portfolio_evaluation_step (portfolio_id, step_index);

-- Structured exclusion detail retained for every candidate that did not make it
-- into the portfolio, so a user is told WHY and what the alternative is.
CREATE TABLE ioe.portfolio_exclusion (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    portfolio_id        uuid NOT NULL REFERENCES ioe.strategy_portfolio(id) ON DELETE CASCADE,
    candidate_id        uuid NOT NULL REFERENCES ioe.optimization_candidate(id) ON DELETE CASCADE,
    membership          text NOT NULL,
    reason_code         text NOT NULL,
    blocking_candidate_id uuid REFERENCES ioe.optimization_candidate(id) ON DELETE SET NULL,
    shared_resource_code  text,
    resolution_options    jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (portfolio_id, candidate_id)
);
CREATE INDEX ix_portfolio_exclusion ON ioe.portfolio_exclusion (portfolio_id);

-- Both new tables are immutable calculation evidence.
CREATE TRIGGER trg_immutable BEFORE UPDATE OR DELETE ON ioe.portfolio_evaluation_step
    FOR EACH ROW EXECUTE FUNCTION ioe.reject_result_mutation();
CREATE TRIGGER trg_immutable BEFORE UPDATE OR DELETE ON ioe.portfolio_exclusion
    FOR EACH ROW EXECUTE FUNCTION ioe.reject_result_mutation();

GRANT SELECT, INSERT, UPDATE, DELETE ON
    ioe.portfolio_evaluation_step, ioe.portfolio_exclusion TO onyx_app_rw;
GRANT SELECT ON
    ioe.portfolio_evaluation_step, ioe.portfolio_exclusion TO onyx_app_ro;
