-- =============================================================================
-- Onyx Ledger — 25 · Rule-authored cost taxonomy alignment  (schema: rules)
-- Alembic revision: 0031_rules_cost_taxonomy
-- IOE architecture Revision 2.1 §B (portfolio objective metrics), P4.
--
-- P4 separates commitment concepts that the legacy vocabulary conflated:
--   liquidity_commitment        cash that must be available (retained value)
--   asset_transfer              value moved in kind (retained value)
--   nonrecoverable_expenditure  money that is gone (reduces the objective)
--
-- `ioe.candidate_cost` accepts the separated taxonomy as of 0030, but a
-- candidate cost is DERIVED from rule data. Unless `rules.rule_action` can also
-- author the separated values, the distinction can never reach the portfolio and
-- the P4 per-concept totals would always collapse back to the legacy member.
--
-- ADDITIVE ONLY: the CHECK is widened, never narrowed. Every legacy value stays
-- valid, so existing published rule versions are untouched.
-- =============================================================================

DO $$
DECLARE c text;
BEGIN
    FOR c IN
        SELECT con.conname FROM pg_constraint con
         WHERE con.conrelid = 'rules.rule_action'::regclass
           AND con.contype = 'c'
           AND con.conkey = ARRAY[(SELECT a.attnum FROM pg_attribute a
                                    WHERE a.attrelid = con.conrelid
                                      AND a.attname = 'cost_type')]
    LOOP
        EXECUTE format('ALTER TABLE rules.rule_action DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE rules.rule_action
    ADD CONSTRAINT rule_action_cost_type_check
    CHECK (cost_type IN (
        -- legacy (retained; required_cash_contribution ≡ liquidity_commitment
        -- + asset_transfer before they were separated)
        'required_cash_contribution','required_expenditure','implementation_cost',
        -- P4 taxonomy
        'liquidity_commitment','asset_transfer','nonrecoverable_expenditure'
    )) NOT VALID;
ALTER TABLE rules.rule_action VALIDATE CONSTRAINT rule_action_cost_type_check;

COMMENT ON COLUMN rules.rule_action.cost_type IS
    'Rule-authored commitment class. Liquidity commitments and asset transfers '
    'constrain feasibility but are not losses; only nonrecoverable expenditure '
    'and implementation cost reduce the portfolio objective.';
