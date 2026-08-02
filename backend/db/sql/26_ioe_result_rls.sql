-- =============================================================================
-- Onyx Ledger — 26 · Row-Level Security for IOE result evidence  (schema: ioe)
-- Alembic revision: 0032_ioe_result_rls
-- IOE architecture Revision 2 §19 (tenant isolation).
--
-- 21_ioe.sql enabled RLS on the two tables that carry `user_id` directly
-- (`optimization_run`, `scenario`) and relied on child evidence being reached
-- "only through an authorized parent". That holds for the application's own
-- query paths, but it is an APPLICATION invariant, not a database one: a direct
-- SELECT on `ioe.strategy_portfolio` by an authenticated user returned another
-- user's portfolio. Optimization results are recommendations about a person's
-- finances, and the isolation requirement is unconditional, so the constraint is
-- moved into the database where it cannot be bypassed by a missing join.
--
-- Each policy resolves ownership through the chain of FKs back to the row that
-- actually carries `user_id`. `ref.current_app_user()` is NULL when no user
-- context is set, so every policy DENIES by default.
--
-- ADDITIVE ONLY: no table, column, or existing policy is altered.
-- =============================================================================

-- ---- reachable from optimization_run ----------------------------------------
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'optimization_candidate', 'optimization_run_event',
        'recommendation_relationship', 'run_rule_version', 'strategy_portfolio'
    ] LOOP
        EXECUTE format('ALTER TABLE ioe.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE ioe.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($f$
            CREATE POLICY p_self_%1$s ON ioe.%1$I
                USING (EXISTS (SELECT 1 FROM ioe.optimization_run r
                                WHERE r.id = run_id
                                  AND r.user_id = ref.current_app_user()))
                WITH CHECK (EXISTS (SELECT 1 FROM ioe.optimization_run r
                                     WHERE r.id = run_id
                                       AND r.user_id = ref.current_app_user()))
        $f$, t);
    END LOOP;
END $$;

-- `multi_year_projection` and `run_rule_snapshot` carry a NULLABLE run_id, so
-- the predicate is written to deny a row that cannot be attributed to a user
-- rather than to leak it.
ALTER TABLE ioe.multi_year_projection ENABLE ROW LEVEL SECURITY;
ALTER TABLE ioe.multi_year_projection FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_multi_year_projection ON ioe.multi_year_projection
    USING (run_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM ioe.optimization_run r
         WHERE r.id = run_id AND r.user_id = ref.current_app_user()))
    WITH CHECK (run_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM ioe.optimization_run r
         WHERE r.id = run_id AND r.user_id = ref.current_app_user()));

-- ---- reachable from optimization_candidate ----------------------------------
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'candidate_cost', 'candidate_economic_effect',
        'confidence_component', 'score_component'
    ] LOOP
        EXECUTE format('ALTER TABLE ioe.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE ioe.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($f$
            CREATE POLICY p_self_%1$s ON ioe.%1$I
                USING (EXISTS (
                    SELECT 1 FROM ioe.optimization_candidate k
                      JOIN ioe.optimization_run r ON r.id = k.run_id
                     WHERE k.id = candidate_id
                       AND r.user_id = ref.current_app_user()))
                WITH CHECK (EXISTS (
                    SELECT 1 FROM ioe.optimization_candidate k
                      JOIN ioe.optimization_run r ON r.id = k.run_id
                     WHERE k.id = candidate_id
                       AND r.user_id = ref.current_app_user()))
        $f$, t);
    END LOOP;
END $$;

-- ---- reachable from strategy_portfolio (P4 evidence) ------------------------
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'portfolio_member', 'portfolio_evaluation_step',
        'portfolio_exclusion', 'resource_ledger_entry'
    ] LOOP
        EXECUTE format('ALTER TABLE ioe.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE ioe.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($f$
            CREATE POLICY p_self_%1$s ON ioe.%1$I
                USING (EXISTS (
                    SELECT 1 FROM ioe.strategy_portfolio p
                      JOIN ioe.optimization_run r ON r.id = p.run_id
                     WHERE p.id = portfolio_id
                       AND r.user_id = ref.current_app_user()))
                WITH CHECK (EXISTS (
                    SELECT 1 FROM ioe.strategy_portfolio p
                      JOIN ioe.optimization_run r ON r.id = p.run_id
                     WHERE p.id = portfolio_id
                       AND r.user_id = ref.current_app_user()))
        $f$, t);
    END LOOP;
END $$;

-- ---- reachable from scenario ------------------------------------------------
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'scenario_event', 'scenario_input_change', 'scenario_result'
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

-- Supporting indexes: each policy resolves ownership by primary key on the
-- parent, so no additional index is required beyond the existing FK indexes.
