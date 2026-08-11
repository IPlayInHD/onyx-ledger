-- =============================================================================
-- Entry 11B6E — SCENARIO_RETENTION: purge what never sealed, keep what did
-- (migration 0062)
-- =============================================================================
--
-- Migration 0060 detached `ioe.scenario` from `identity.user_account` so a
-- sealed scenario would survive account removal. It has no way to tell a sealed
-- scenario from a draft, so BOTH survive — and a `pending` scenario carrying
-- `label = 'my divorce settlement'` outliving its owner is not retention, it is
-- a leak. 0060's own analysis recorded that as BRANCH_CLEANUP_PENDING. This is
-- that cleanup.
--
-- THE DISCRIMINATOR IS `scenario_result_hash`, not `workflow_status` and not
-- whether a verification happens to have been run. A scenario with a result
-- hash sealed something replay can verify; one without never produced evidence
-- at all, whatever its workflow state says. Keying on the seal rather than on
-- the workflow means a `failed` or `cancelled` scenario is purged (it sealed
-- nothing) while a `completed` one is kept, which is what the evidence
-- semantics actually require.
--
-- WHY A NEW PHASE. SOURCE_DATA covers live financial, profile and wealth rows
-- and nothing else — `count_remaining_source_data` names eight tables and no
-- scenario table is among them. Widening it would silently change what an
-- already-recorded COMPLETE means for every account that has finished it.
-- AUDIT_AUTH_DEIDENTIFICATION is identity severance on audit and auth history;
-- mixing tax-evidence cleanup into it would blur two very different guarantees.
-- So this is its own phase, with its own durable progress and its own guard.

-- ---------------------------------------------------------------------------
-- 1. The phase exists
-- ---------------------------------------------------------------------------
-- The constraint is `ck_lifecycle_phase_name`, from 45_source_data_purge.sql.
-- Read from the catalogue rather than guessed: the first draft invented
-- `account_lifecycle_phase_phase_check`, which PostgreSQL happily created
-- alongside the real one, leaving two CHECKs where the older and stricter one
-- still rejected every SCENARIO_RETENTION row.
ALTER TABLE identity.account_lifecycle_phase
    DROP CONSTRAINT IF EXISTS ck_lifecycle_phase_name;
ALTER TABLE identity.account_lifecycle_phase
    ADD CONSTRAINT ck_lifecycle_phase_name
    CHECK (phase IN ('SOURCE_DATA', 'DOCUMENTS', 'AUDIT_AUTH_DEIDENTIFICATION',
                     'SCENARIO_RETENTION'));

-- ---------------------------------------------------------------------------
-- 2. The completion guard
-- ---------------------------------------------------------------------------
-- Two independent categories, counted separately so a caller can see which
-- half is outstanding, and summed so the phase has one number to be zero.
--
-- `label` and `note` are the only user-authored free text on the retained row.
-- Everything else in the scenario branch is a registered code, a typed lever
-- value, a hash or a timestamp — `scenario_assumption.value_text` and
-- `scenario_input_change.old_value/new_value` are lever values produced by the
-- engine from a validated spec, not prose somebody typed.
CREATE OR REPLACE FUNCTION identity.count_remaining_scenario_privacy_work(
    p_user_id uuid
)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = ioe, pg_catalog
AS $$
    SELECT (
        -- Never sealed anything: it is live product state and must not outlive
        -- the account.
        (SELECT count(*) FROM ioe.scenario
          WHERE user_id = p_user_id AND scenario_result_hash IS NULL)
        -- Sealed, therefore retained — but still carrying free text.
      + (SELECT count(*) FROM ioe.scenario
          WHERE user_id = p_user_id AND scenario_result_hash IS NOT NULL
            AND (label IS NOT NULL OR note IS NOT NULL))
    )::integer
$$;

ALTER FUNCTION identity.count_remaining_scenario_privacy_work(uuid)
    OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION identity.count_remaining_scenario_privacy_work(uuid)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.count_remaining_scenario_privacy_work(uuid)
    TO onyx_privacy_worker;

COMMENT ON FUNCTION identity.count_remaining_scenario_privacy_work(uuid) IS
    'Entry 11B6E. Scenario work still owed before terminal account removal: unsealed scenarios that must be deleted, plus sealed scenarios still carrying user free text. Counts obligations, never retained evidence — a sealed scenario with no label is finished, not outstanding.';

-- ---------------------------------------------------------------------------
-- 3. The keyhole
-- ---------------------------------------------------------------------------
-- One entry point for one subject, authorised by the live claim exactly like
-- `purge_source_data` and `deidentify_audit_auth`. The privacy worker holds
-- EXECUTE on this and no DELETE or UPDATE on any scenario table, so "delete
-- every user's drafts" is not expressible by the role that runs it.
--
-- The DELETE runs inside `app.allow_evidence_purge`, which is the sanctioned
-- context `ioe.reject_result_mutation` yields to. That is deliberate rather
-- than a workaround: an unsealed scenario's own event rows are immutable
-- calculation evidence about a calculation that produced nothing, and the
-- purge context is the mechanism the schema already provides for removing
-- them. Sealed scenarios are excluded from the DELETE by the WHERE clause, so
-- the context is never used to destroy anything that sealed evidence.
CREATE OR REPLACE FUNCTION identity.prepare_scenario_retention(
    p_user_id     uuid,
    p_claim_token uuid,
    p_worker_id   text
)
RETURNS TABLE (out_purged integer, out_sanitized integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, ioe, pg_catalog
AS $$
DECLARE
    v_purged    integer := 0;
    v_sanitized integer := 0;
BEGIN
    PERFORM 1 FROM identity.account_lifecycle
     WHERE user_id = p_user_id
       AND claim_token = p_claim_token
       AND claimed_by = p_worker_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no live claim for this subject'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    PERFORM set_config('app.allow_evidence_purge', 'on', true);

    WITH gone AS (
        DELETE FROM ioe.scenario
         WHERE user_id = p_user_id
           AND scenario_result_hash IS NULL
        RETURNING 1)
    SELECT count(*) INTO v_purged FROM gone;

    PERFORM set_config('app.allow_evidence_purge', 'off', true);

    -- Sealed rows keep every byte of their evidence. `label` and `note` are
    -- outside the sealed envelope by construction: ScenarioSpec holds them for
    -- convenience and excludes them from `canonical_for_hash()`, precisely so
    -- renaming a scenario cannot change its identity or invalidate it. Clearing
    -- them therefore cannot move a hash, and the sealing trigger agrees — it
    -- lists neither among the columns it seals on completion.
    WITH cleaned AS (
        UPDATE ioe.scenario
           SET label = NULL, note = NULL
         WHERE user_id = p_user_id
           AND scenario_result_hash IS NOT NULL
           AND (label IS NOT NULL OR note IS NOT NULL)
        RETURNING 1)
    SELECT count(*) INTO v_sanitized FROM cleaned;

    RETURN QUERY SELECT v_purged, v_sanitized;
END;
$$;

ALTER FUNCTION identity.prepare_scenario_retention(uuid, uuid, text)
    OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION identity.prepare_scenario_retention(uuid, uuid, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.prepare_scenario_retention(uuid, uuid, text)
    FROM onyx_app_rw, onyx_app_ro;
GRANT EXECUTE ON FUNCTION identity.prepare_scenario_retention(uuid, uuid, text)
    TO onyx_privacy_worker;

COMMENT ON FUNCTION identity.prepare_scenario_retention(uuid, uuid, text) IS
    'Entry 11B6E. Deletes one subject''s scenarios that never sealed a result, and clears label/note on the ones that did. Sealed evidence is never deleted and never has a sealed column written. Authorised by the live claim token, like every other privacy keyhole.';

-- ---------------------------------------------------------------------------
-- 4. Completion refuses while work remains
-- ---------------------------------------------------------------------------
-- Same shape as SOURCE_DATA and AUDIT_AUTH_DEIDENTIFICATION: enforced in the
-- database so that no worker — this one, or whatever replaces it — can record
-- a phase done that is not.
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

    UPDATE identity.account_lifecycle_phase
       SET status = 'COMPLETE', completed_at = now(),
           last_failure_code = NULL, updated_at = now()
     WHERE user_id = p_user_id AND phase = p_phase;
    RETURN FOUND;
END;
$$;

-- ---------------------------------------------------------------------------
-- 5. Sanitized free text stays sanitized while the account is being deleted
-- ---------------------------------------------------------------------------
-- Found by trying it, in the §29 resurrection test. The account-lifecycle
-- cutoff that stops a deleting account from acting lives in the SERVICE layer
-- — `AccountDeletionInProgress` is raised by application code — so it does not
-- constrain a statement issued directly against the database. The application
-- role holds UPDATE on `ioe.scenario` (correctly: renaming a scenario is an
-- ordinary product feature) and RLS scopes it to the subject, so:
--
--     UPDATE ioe.scenario SET label='restored' WHERE id=<sanitized>;   -- succeeded
--
-- after SCENARIO_RETENTION had completed. Terminal account removal will be
-- gated on that COMPLETE, so free text reappearing behind it is the same
-- failure shape 11B6D found on `identity.login_event`.
--
-- This file's own module docstring already states the repository's position:
-- the service is the API everything should use, and the database constraint is
-- what makes "everything" true.
--
-- The guard is deliberately narrow. It refuses only the transition to NON-NULL
-- free text, and only while a lifecycle row exists for the owner — that is,
-- only for accounts that have asked to be deleted. Ordinary renaming is
-- untouched, and the phase's own write (to NULL) is untouched.
CREATE OR REPLACE FUNCTION ioe.guard_scenario_text_after_deletion_request()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF (NEW.label IS NOT NULL AND NEW.label IS DISTINCT FROM OLD.label)
       OR (NEW.note IS NOT NULL AND NEW.note IS DISTINCT FROM OLD.note) THEN
        IF EXISTS (SELECT 1 FROM identity.account_lifecycle
                    WHERE user_id = NEW.user_id) THEN
            RAISE EXCEPTION
                'ioe.scenario %: free text cannot be set while the account is being deleted',
                NEW.id
                USING ERRCODE = 'raise_exception';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

ALTER FUNCTION ioe.guard_scenario_text_after_deletion_request()
    OWNER TO onyx_migrator;
REVOKE EXECUTE ON FUNCTION ioe.guard_scenario_text_after_deletion_request()
    FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_scenario_text_deletion_guard ON ioe.scenario;
CREATE TRIGGER trg_scenario_text_deletion_guard
    BEFORE UPDATE ON ioe.scenario
    FOR EACH ROW EXECUTE FUNCTION ioe.guard_scenario_text_after_deletion_request();

COMMENT ON FUNCTION ioe.guard_scenario_text_after_deletion_request() IS
    'Entry 11B6E. Once an account has requested deletion, user free text cannot be written onto its scenarios. The lifecycle cutoff is enforced in the service layer and does not bind a direct statement, so without this a sanitized retained scenario could regain a label after SCENARIO_RETENTION was recorded COMPLETE.';
