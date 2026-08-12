-- =============================================================================
-- Entry 11B6I (follow-up) — the HISTORICAL_DETAIL_CLEANUP write cutoff
-- =============================================================================
--
-- 11B6I closed the fifteen derived-detail tables with a lifecycle phase, and
-- left one gap open and documented: after the phase reported COMPLETE, a writer
-- holding INSERT could put the detail straight back. Through the product that
-- is unreachable — the engine runs only for an authenticated user and a
-- deleting account is ACCESS_DISABLED — but nothing refused it at the database
-- level, and "unreachable through the paths we thought of" is not a guarantee.
-- This is the guarantee.
--
-- WHY THIS IS A STATEMENT-LEVEL TRIGGER, NOT A ROW-LEVEL ONE.
--
-- The reason the gap was left open in 11B6I was cost: these are the hottest
-- write paths in the system. One ordinary optimization writes thousands of
-- `ioe.score_component` rows, and a FOR EACH ROW trigger would add a lifecycle
-- lookup to every one of them.
--
-- A transition table removes the objection instead of accepting it. `REFERENCING
-- NEW TABLE` hands the whole inserted batch to ONE invocation, so the check is a
-- single semi-join per statement no matter how many rows the statement carried —
-- O(statements), not O(rows). That is why this could be built properly rather
-- than either shipped slow or left undone.
--
-- MEASURED, NOT ASSUMED. Interleaving the two conditions across six rounds of a
-- full `OptimizationOrchestrator.generate` writing ~1,400 detail rows:
--
--     cutoff ENABLED    median  542.5 ms
--     cutoff DISABLED   median  469.7 ms
--     overhead          +72.8 ms  (+15.5%)
--
-- Interleaved because the first attempt ran all-enabled then all-disabled and
-- measured the growing rule landscape instead of the trigger — it reported the
-- guard making things 16% FASTER, which is how a benchmark tells you it is
-- measuring the wrong thing.
--
-- 73 ms per optimization, on a background generation path, for a hard guarantee
-- that purged personal detail cannot be written back. Recorded here so the
-- trade is visible rather than folded into a claim that it is free.
--
-- WHAT THE CUTOFF IS. The existence of a row in `identity.account_lifecycle`,
-- which is exactly the test `ioe.guard_scenario_text_after_deletion_request`
-- already uses for scenario free text (Entry 11B6E). An account with no
-- lifecycle row is active, so ordinary accounts are unaffected and pay one
-- semi-join that finds nothing.
--
-- The refusal is deliberately EARLY — from the moment deletion is requested, not
-- from the moment the cleanup phase completes. A late write that lands between
-- the request and the phase would otherwise be purged silently, and one that
-- lands after would resurrect detail. Refusing from the request covers both, and
-- matches §30's preference for preventing creation over reconciling it.
--
-- WHAT IT DOES NOT TOUCH. The eight tables integrity verification reads are NOT
-- listed here. They are retained evidence: they are never purged, so there is
-- nothing to resurrect, and a cutoff on them would refuse writes that a sealed
-- artifact legitimately needs.

-- ---------------------------------------------------------------------------
-- 1. One function per ownership shape
-- ---------------------------------------------------------------------------
-- Five shapes, because the path from a detail row to its account differs. Each
-- is a single set-based EXISTS over the transition table.

CREATE OR REPLACE FUNCTION ioe.reject_run_detail_after_deletion_request()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM new_rows n
          JOIN ioe.optimization_run r ON r.id = n.run_id
          JOIN identity.account_lifecycle al ON al.user_id = r.user_id
    ) THEN
        RAISE EXCEPTION
            '%: derived detail cannot be written while the account is being deleted',
            TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ioe.reject_candidate_detail_after_deletion_request()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM new_rows n
          JOIN ioe.optimization_candidate c ON c.id = n.candidate_id
          JOIN ioe.optimization_run r ON r.id = c.run_id
          JOIN identity.account_lifecycle al ON al.user_id = r.user_id
    ) THEN
        RAISE EXCEPTION
            '%: derived detail cannot be written while the account is being deleted',
            TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ioe.reject_portfolio_detail_after_deletion_request()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM new_rows n
          JOIN ioe.strategy_portfolio p ON p.id = n.portfolio_id
          JOIN ioe.optimization_run r ON r.id = p.run_id
          JOIN identity.account_lifecycle al ON al.user_id = r.user_id
    ) THEN
        RAISE EXCEPTION
            '%: derived detail cannot be written while the account is being deleted',
            TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ioe.reject_scenario_detail_after_deletion_request()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM new_rows n
          JOIN ioe.scenario s ON s.id = n.scenario_id
          JOIN identity.account_lifecycle al ON al.user_id = s.user_id
    ) THEN
        RAISE EXCEPTION
            '%: derived detail cannot be written while the account is being deleted',
            TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION analysis.reject_analysis_detail_after_deletion_request()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM new_rows n
          JOIN analysis.analysis_run a ON a.id = n.analysis_id
          JOIN identity.account_lifecycle al ON al.user_id = a.user_id
    ) THEN
        RAISE EXCEPTION
            '%: derived detail cannot be written while the account is being deleted',
            TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. Attach one trigger per purged table
-- ---------------------------------------------------------------------------
-- Written out rather than generated in a loop, for the same reason the purge
-- keyhole names its tables: the list IS the security property, and a loop over
-- a catalogue query would silently follow the schema wherever it went.

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.candidate_cost;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.candidate_cost
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_candidate_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.candidate_economic_effect;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.candidate_economic_effect
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_candidate_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.confidence_component;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.confidence_component
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_candidate_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.score_component;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.score_component
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_candidate_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.recommendation_relationship;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.recommendation_relationship
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_run_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.multi_year_projection;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.multi_year_projection
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_run_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.optimization_run_event;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.optimization_run_event
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_run_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.portfolio_evaluation_step;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.portfolio_evaluation_step
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_portfolio_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.scenario_result;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.scenario_result
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_scenario_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.scenario_input_change;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.scenario_input_change
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_scenario_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.scenario_confidence_component;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.scenario_confidence_component
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_scenario_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON ioe.scenario_event;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON ioe.scenario_event
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION ioe.reject_scenario_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON analysis.analysis_line_item;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON analysis.analysis_line_item
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION analysis.reject_analysis_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON analysis.analysis_assumption;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON analysis.analysis_assumption
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION analysis.reject_analysis_detail_after_deletion_request();

DROP TRIGGER IF EXISTS trg_write_cutoff ON analysis.reconciliation_check;
CREATE TRIGGER trg_write_cutoff
    AFTER INSERT ON analysis.reconciliation_check
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION analysis.reject_analysis_detail_after_deletion_request();
