-- =============================================================================
-- Entry 11B6I — the HISTORICAL_DETAIL_CLEANUP phase (migration 0064)
-- =============================================================================
--
-- The four certified phases empty the live source data, the documents, the
-- unsealed scenario text and the attributable audit/auth trail. What none of
-- them touches is the *derived detail* hanging off the sealed roots: fifteen
-- tables of per-person tax computation that survive every phase and, since
-- migration 0060 detached the roots from `identity.user_account`, would outlive
-- the account entirely, carrying a `user_id` that no longer resolves to a
-- person. Nothing governed them in either direction. This phase governs them.
--
-- WHAT THIS PHASE IS NOT. It is emphatically NOT "delete everything below the
-- sealed root". Entry 11B6H measured that integrity verification READS eight of
-- the twenty `ioe` sealed-detail tables and rebuilds the hashed canonical form
-- from their rows — `optimization_candidate`, `portfolio_member`,
-- `portfolio_exclusion`, `resource_ledger_entry`, `run_rule_version`,
-- `scenario_lever`, `scenario_assumption`, `strategy_portfolio`. Deleting any
-- of them turns a verified artifact into a mismatch, which is the outcome
-- indistinguishable from tampering.
--
-- SO THE TABLE LIST BELOW IS WRITTEN OUT BY NAME, AND WALKING THE PARENT IS
-- FORBIDDEN. A keyhole that recursed through `optimization_run`'s children
-- would reach `portfolio_member` and destroy evidence. The explicitness is the
-- safety property, not an inconvenience:
--
--     PURGE_SET  ∩  VERIFICATION_REQUIRED_SET  =  ∅
--
-- and `tests/privacy/test_historical_detail_cleanup.py` asserts exactly that
-- against the two registries rather than against this comment.
--
-- WHY THESE FIFTEEN MAY GO — AND WHY THE ARGUMENT IS *NOT* REBUILDABILITY.
--
-- An earlier draft of this file claimed eight of them were reproducible because
-- their values are committed into `optimization_result_hash` /
-- `scenario_result_hash`. THAT REASONING WAS WRONG and is recorded here so it
-- is not reintroduced: a digest commits to a preimage, it does not permit
-- reconstructing one. Neither root stores a structured result payload —
-- `ioe.optimization_run` and `ioe.scenario` hold digests plus a few aggregates,
-- and the detailed values exist ONLY in the tables below.
--
-- The one remaining reconstruction route is re-executing the engine over the
-- retained inputs, and that route is NOT durable. Measured: with the pins
-- untouched and the running build's `tax_engine_version` moved on, all three
-- replays return `unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE`. Recomputation
-- works only while the build that produced the evidence is still the build that
-- is running, which is a transient property by design.
--
-- So none of these fifteen is classified DERIVED_DELETE. They are all
-- LIVE_USER_DATA_DELETE, and the rationale is the one that does not depend on
-- reconstructability at all:
--
--     NOTHING READS THEM ONCE THE ACCOUNT IS GONE.
--
-- Measured, not assumed: twelve have no reader anywhere in the codebase, and
-- the three that do — `multi_year_projection`, `scenario_input_change`,
-- `scenario_result` — are reached only by account-scoped product routes behind
-- `Depends(current_user_id)` under RLS. No operator, security, audit, export or
-- background consumer exists for any of them. Non-reconstructable is not a
-- retention reason; an independent post-account reader would have been, and
-- there is none.
--
-- All fifteen are insert-only under `ioe.reject_result_mutation` (the twelve
-- `ioe` ones) and refuse DELETE outside the sanctioned purge context. That
-- trigger's own comment anticipated this phase: DELETE is permitted inside the
-- purge context "so the account-erasure path (privacy/retention) can run", and
-- until now no such path existed for this branch.

-- ---------------------------------------------------------------------------
-- 1. The phase name
-- ---------------------------------------------------------------------------
-- `ck_lifecycle_phase_name`, from 45_source_data_purge.sql and widened by 56.
-- Read from the catalogue, never invented: a second CHECK created alongside the
-- real one leaves the older, stricter one rejecting every row of the new phase.
ALTER TABLE identity.account_lifecycle_phase
    DROP CONSTRAINT IF EXISTS ck_lifecycle_phase_name;
ALTER TABLE identity.account_lifecycle_phase
    ADD CONSTRAINT ck_lifecycle_phase_name
    CHECK (phase IN ('SOURCE_DATA', 'DOCUMENTS', 'AUDIT_AUTH_DEIDENTIFICATION',
                     'SCENARIO_RETENTION', 'HISTORICAL_DETAIL_CLEANUP'));

-- ---------------------------------------------------------------------------
-- 2. The completion guard
-- ---------------------------------------------------------------------------
-- One number, summed over every table in the purge set. Set-based: a per-row
-- loop here would be a second place for the table list to drift from the purge.
--
-- EVERY GROUP IS COUNTED. The guard-on-the-guard test reinserts one
-- representative row per semantic group and requires this to become non-zero,
-- so a branch missing from this query fails a test rather than silently
-- certifying a phase that left rows behind.
CREATE OR REPLACE FUNCTION identity.count_remaining_historical_detail(
    p_user_id uuid
)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ioe, analysis, pg_catalog
AS $$
    WITH runs AS (
        SELECT id FROM ioe.optimization_run WHERE user_id = p_user_id
    ), cands AS (
        SELECT c.id FROM ioe.optimization_candidate c
          JOIN runs r ON r.id = c.run_id
    ), ports AS (
        SELECT p.id FROM ioe.strategy_portfolio p
          JOIN runs r ON r.id = p.run_id
    ), scens AS (
        SELECT id FROM ioe.scenario WHERE user_id = p_user_id
    ), anas AS (
        SELECT id FROM analysis.analysis_run WHERE user_id = p_user_id
    )
    SELECT
        (SELECT count(*) FROM ioe.candidate_cost
          WHERE candidate_id IN (SELECT id FROM cands))
      + (SELECT count(*) FROM ioe.candidate_economic_effect
          WHERE candidate_id IN (SELECT id FROM cands))
      + (SELECT count(*) FROM ioe.confidence_component
          WHERE candidate_id IN (SELECT id FROM cands))
      + (SELECT count(*) FROM ioe.score_component
          WHERE candidate_id IN (SELECT id FROM cands))
      + (SELECT count(*) FROM ioe.recommendation_relationship
          WHERE run_id IN (SELECT id FROM runs))
      + (SELECT count(*) FROM ioe.multi_year_projection
          WHERE run_id IN (SELECT id FROM runs))
      + (SELECT count(*) FROM ioe.optimization_run_event
          WHERE run_id IN (SELECT id FROM runs))
      + (SELECT count(*) FROM ioe.portfolio_evaluation_step
          WHERE portfolio_id IN (SELECT id FROM ports))
      + (SELECT count(*) FROM ioe.scenario_result
          WHERE scenario_id IN (SELECT id FROM scens))
      + (SELECT count(*) FROM ioe.scenario_input_change
          WHERE scenario_id IN (SELECT id FROM scens))
      + (SELECT count(*) FROM ioe.scenario_confidence_component
          WHERE scenario_id IN (SELECT id FROM scens))
      + (SELECT count(*) FROM ioe.scenario_event
          WHERE scenario_id IN (SELECT id FROM scens))
      + (SELECT count(*) FROM analysis.analysis_line_item
          WHERE analysis_id IN (SELECT id FROM anas))
      + (SELECT count(*) FROM analysis.analysis_assumption
          WHERE analysis_id IN (SELECT id FROM anas))
      + (SELECT count(*) FROM analysis.reconciliation_check
          WHERE analysis_id IN (SELECT id FROM anas))
$$;

COMMENT ON FUNCTION identity.count_remaining_historical_detail(uuid) IS
    'Rows of account-owned derived detail not required by the retained sealed '
    'evidence contract. HISTORICAL_DETAIL_CLEANUP refuses to complete while '
    'this is non-zero. Never counts a table the verifier reads.';

-- ---------------------------------------------------------------------------
-- 3. The keyhole
-- ---------------------------------------------------------------------------
-- Account-scoped, claim-authorised, and the ONLY way `onyx_privacy_worker` can
-- delete any of this. The role holds no DELETE on these tables directly, so the
-- capability to erase ONE account's detail never becomes the capability to
-- express "every account's".
--
-- THE GUC IS NOT THE AUTHORISATION BOUNDARY. `app.allow_evidence_purge` is set
-- LOCAL inside this function purely so `ioe.reject_result_mutation` permits the
-- DELETE it is already authorised to perform. Any role may issue `SET
-- app.allow_evidence_purge = 'on'` — that is what makes it useless as a
-- control, and why effective table privilege has to be the real boundary.
-- `test_the_guc_is_not_the_authorisation_boundary` proves from a genuine
-- `onyx_app_rw` LOGIN that setting it grants nothing.
CREATE OR REPLACE FUNCTION identity.purge_historical_detail(
    p_user_id     uuid,
    p_claim_token uuid,
    p_worker_id   text
)
RETURNS TABLE (out_table text, out_deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, ioe, analysis, pg_catalog
AS $$
DECLARE
    v_prior text;
    v_n     bigint;
BEGIN
    -- The claim token is the authority, exactly as in purge_source_data. A
    -- worker whose lease expired and was reclaimed cannot purge the subject it
    -- no longer holds.
    PERFORM 1 FROM identity.account_lifecycle
      WHERE user_id = p_user_id AND claim_token = p_claim_token
      FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'purge refused: the claim token does not hold this subject';
    END IF;

    -- Run as the tenant, so RLS confines every statement below: a WHERE clause
    -- that forgot `user_id` still could not reach another tenant.
    v_prior := current_setting('app.user_id', true);
    PERFORM set_config('app.user_id', p_user_id::text, true);
    -- Transaction-local, and only so the insert-only trigger permits a DELETE
    -- this function is already authorised to make.
    PERFORM set_config('app.allow_evidence_purge', 'on', true);

    -- Candidate-owned detail. `optimization_candidate` ITSELF IS NOT TOUCHED —
    -- the verifier resolves candidate keys through it to rebuild the portfolio
    -- hash.
    DELETE FROM ioe.candidate_cost
     WHERE candidate_id IN (SELECT c.id FROM ioe.optimization_candidate c
                              JOIN ioe.optimization_run r ON r.id = c.run_id
                             WHERE r.user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.candidate_cost'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM ioe.candidate_economic_effect
     WHERE candidate_id IN (SELECT c.id FROM ioe.optimization_candidate c
                              JOIN ioe.optimization_run r ON r.id = c.run_id
                             WHERE r.user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.candidate_economic_effect'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM ioe.confidence_component
     WHERE candidate_id IN (SELECT c.id FROM ioe.optimization_candidate c
                              JOIN ioe.optimization_run r ON r.id = c.run_id
                             WHERE r.user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.confidence_component'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM ioe.score_component
     WHERE candidate_id IN (SELECT c.id FROM ioe.optimization_candidate c
                              JOIN ioe.optimization_run r ON r.id = c.run_id
                             WHERE r.user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.score_component'; out_deleted := v_n; RETURN NEXT;

    -- Run-owned detail.
    DELETE FROM ioe.recommendation_relationship
     WHERE run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.recommendation_relationship'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM ioe.multi_year_projection
     WHERE run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.multi_year_projection'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM ioe.optimization_run_event
     WHERE run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.optimization_run_event'; out_deleted := v_n; RETURN NEXT;

    -- Portfolio-owned detail. `strategy_portfolio`, `portfolio_member`,
    -- `portfolio_exclusion` and `resource_ledger_entry` ARE NOT TOUCHED.
    DELETE FROM ioe.portfolio_evaluation_step
     WHERE portfolio_id IN (SELECT p.id FROM ioe.strategy_portfolio p
                              JOIN ioe.optimization_run r ON r.id = p.run_id
                             WHERE r.user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.portfolio_evaluation_step'; out_deleted := v_n; RETURN NEXT;

    -- Scenario-owned detail. `scenario_lever` and `scenario_assumption` ARE NOT
    -- TOUCHED — they are the sealed spec the verifier reads back.
    DELETE FROM ioe.scenario_result
     WHERE scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.scenario_result'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM ioe.scenario_input_change
     WHERE scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.scenario_input_change'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM ioe.scenario_confidence_component
     WHERE scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.scenario_confidence_component'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM ioe.scenario_event
     WHERE scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'ioe.scenario_event'; out_deleted := v_n; RETURN NEXT;

    -- Analysis-owned detail. `analysis_input_snapshot` IS NOT TOUCHED — it is
    -- the frozen baseline every replay resolves.
    DELETE FROM analysis.analysis_line_item
     WHERE analysis_id IN (SELECT id FROM analysis.analysis_run WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'analysis.analysis_line_item'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM analysis.analysis_assumption
     WHERE analysis_id IN (SELECT id FROM analysis.analysis_run WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'analysis.analysis_assumption'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM analysis.reconciliation_check
     WHERE analysis_id IN (SELECT id FROM analysis.analysis_run WHERE user_id = p_user_id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'analysis.reconciliation_check'; out_deleted := v_n; RETURN NEXT;

    PERFORM set_config('app.user_id', coalesce(v_prior, ''), true);
    RETURN;
END;
$$;

COMMENT ON FUNCTION identity.purge_historical_detail(uuid, uuid, text) IS
    'Account-scoped removal of derived historical detail not required by the '
    'retained sealed evidence contract. Names every table explicitly; never '
    'walks a parent, because walking optimization_run would reach the tables '
    'integrity verification reads.';

-- ---------------------------------------------------------------------------
-- 4. Completion refuses while work remains
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION identity.complete_lifecycle_phase(
    p_user_id uuid, p_phase text, p_claim_token uuid, p_worker_id text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
DECLARE
    v_remaining bigint;
BEGIN
    PERFORM 1 FROM identity.account_lifecycle
      WHERE user_id = p_user_id AND claim_token = p_claim_token FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF p_phase = 'SOURCE_DATA' THEN
        v_remaining := identity.count_remaining_source_data(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'SOURCE_DATA cannot complete: % in-scope rows remain', v_remaining;
        END IF;
    END IF;

    IF p_phase = 'AUDIT_AUTH_DEIDENTIFICATION' THEN
        v_remaining := identity.count_attributable_audit_auth(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'AUDIT_AUTH_DEIDENTIFICATION cannot complete: % attributable rows remain',
                v_remaining;
        END IF;
    END IF;

    IF p_phase = 'SCENARIO_RETENTION' THEN
        v_remaining := identity.count_remaining_scenario_privacy_work(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'SCENARIO_RETENTION cannot complete: % scenario rows remain', v_remaining;
        END IF;
    END IF;

    IF p_phase = 'DOCUMENTS' THEN
        v_remaining := identity.count_remaining_document_privacy_work(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'DOCUMENTS cannot complete: % document rows remain', v_remaining;
        END IF;
    END IF;

    IF p_phase = 'HISTORICAL_DETAIL_CLEANUP' THEN
        v_remaining := identity.count_remaining_historical_detail(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'HISTORICAL_DETAIL_CLEANUP cannot complete: % detail rows remain',
                v_remaining;
        END IF;
    END IF;

    UPDATE identity.account_lifecycle_phase
       SET status = 'COMPLETE', completed_at = now(),
           last_failure_code = NULL, updated_at = now()
     WHERE user_id = p_user_id AND phase = p_phase;
    RETURN FOUND;
END;
$$;

-- ---------------------------------------------------------------------------
-- 5. Privileges
-- ---------------------------------------------------------------------------
-- MAKING EFFECTIVE PRIVILEGE THE BOUNDARY AGAIN.
--
-- Measured while building this phase: `onyx_app_rw` held DELETE on all
-- twenty-three sealed-detail tables — the fifteen purged here AND the eight
-- integrity verification reads. The only thing refusing the statement was
-- `ioe.reject_result_mutation`, and ANY role may turn that off:
--
--     SET app.allow_evidence_purge = 'on';
--     DELETE FROM ioe.scenario_result WHERE ...;      -- succeeded
--
-- Confirmed from a genuine LOGIN, not by reading migration text. That made a
-- session GUC the de-facto authorisation boundary for destroying sealed replay
-- evidence, bounded only by RLS to the caller's own tenant. A GUC is not a
-- privilege; it is a request.
--
-- No application code sets that GUC — only the SECURITY DEFINER keyholes do,
-- and they run as the owner — so the application role has never had a working
-- delete path here. Revoking it removes a capability nothing uses and closes
-- the hole: with no DELETE privilege, setting the GUC grants nothing.
--
-- SCOPED TO THE `ioe` SEALED TABLES, AND DELIBERATELY NOT TO THE THREE
-- `analysis` ONES. Those carry no `reject_result_mutation` trigger, so the GUC
-- was never their boundary and nothing here was ever false about them: they are
-- ordinary tenant data whose CRUD is the PD-1 model, with RLS as the isolation
-- boundary that `test_one_tenant_cannot_delete_anothers_rows` proves. Revoking
-- DELETE there would break a certified invariant to fix a hole that does not
-- exist on those tables.
--
-- `onyx_privacy_worker` is deliberately NOT granted DELETE either. It reaches
-- these rows only through `identity.purge_historical_detail`, which is
-- account-scoped and claim-authorised, so "erase one account's detail" never
-- becomes "erase everyone's".
REVOKE DELETE ON
    ioe.candidate_cost,
    ioe.candidate_economic_effect,
    ioe.confidence_component,
    ioe.score_component,
    ioe.recommendation_relationship,
    ioe.multi_year_projection,
    ioe.optimization_run_event,
    ioe.portfolio_evaluation_step,
    ioe.scenario_result,
    ioe.scenario_input_change,
    ioe.scenario_confidence_component,
    ioe.scenario_event,
    ioe.optimization_candidate,
    ioe.portfolio_member,
    ioe.portfolio_exclusion,
    ioe.resource_ledger_entry,
    ioe.run_rule_version,
    ioe.scenario_lever,
    ioe.scenario_assumption,
    ioe.strategy_portfolio
FROM onyx_app_rw;

REVOKE ALL ON FUNCTION identity.purge_historical_detail(uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.count_remaining_historical_detail(uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION identity.purge_historical_detail(uuid, uuid, text)
    TO onyx_privacy_worker;
GRANT EXECUTE ON FUNCTION identity.count_remaining_historical_detail(uuid)
    TO onyx_privacy_worker;
