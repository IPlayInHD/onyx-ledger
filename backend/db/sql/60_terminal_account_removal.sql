-- =============================================================================
-- Entry 11B6J — terminal account removal (migration 0066)
-- =============================================================================
--
-- Every entry from 11B6C onward forbade this function by name. It is written
-- now because the thing it was waiting for finally exists: five certified
-- lifecycle phases, each with a completion guard the database itself enforces,
-- and a privacy universe with zero unresolved engineering surfaces.
--
-- PRODUCTION ENABLEMENT IS OFF. `workers/tasks/privacy.py` does not call this
-- and must not: the dispatcher stops at the five phases exactly as before. The
-- capability exists, is proven, and is invoked only deliberately. Physical
-- removal of a person's account is not something a scheduled worker should do
-- because a queue happened to reach it, and PD-15 is still open.
--
-- WHAT MAKES THIS SAFE TO EXIST AT ALL — the guard is fail-closed on SIX
-- independent facts, every one of them read from the database at call time:
--
--   1. the claim token holds this subject          (no stolen lease)
--   2. the lifecycle is in PURGING                 (no skipping the machine)
--   3. all five phases report COMPLETE             (durable record, not memory)
--   4. all five completion guards read zero        (the phases told the truth)
--   5. the account still exists                    (idempotent, not a lie)
--   6. the transition PURGING -> COMPLETE is legal (trigger, 41_account_lifecycle)
--
-- Point 4 is the one that matters most. A phase row saying COMPLETE is a
-- claim; the counts are the evidence. This re-reads the evidence rather than
-- trusting the claim, because the whole series has been a demonstration that
-- those two things come apart.
--
-- WHAT SURVIVES, BY DESIGN AND BY MEASUREMENT:
--
--   * the four sealed roots — 0060 detached them from `identity.user_account`,
--     so no cascade reaches `analysis_run`, `optimization_run`, `scenario` or
--     `integrity_check`, and replay keeps working after the person is gone;
--   * `identity.account_lifecycle` and `account_lifecycle_phase` — no foreign
--     key to the account, so the durable record that a deletion happened
--     outlives the account (PD-9);
--   * `identity.deletion_subject` — subject_key and retired_at only, no
--     user_id, so the tombstone cannot be joined back to a person;
--   * `audit.audit_log` — append-only, and `actor_id` on historical rows is
--     PD-15's remaining residue. THIS FUNCTION DOES NOT TOUCH IT. Rewriting
--     immutable audit history to make a privacy number look better would be
--     the worst thing in this file.
--
-- Thirty inbound foreign keys cascade and four SET NULL; by the time this runs
-- the phases have already emptied or severed every one of them, which is what
-- the completion guards prove.

CREATE OR REPLACE FUNCTION identity.terminal_remove_account(
    p_user_id     uuid,
    p_claim_token uuid,
    p_worker_id   text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
DECLARE
    v_state     text;
    v_phase     text;
    v_remaining bigint;
    v_missing   text;
BEGIN
    -- 1. The claim token is the authority, as in every other privacy keyhole.
    SELECT state INTO v_state
      FROM identity.account_lifecycle
     WHERE user_id = p_user_id AND claim_token = p_claim_token
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'terminal removal refused: the claim token does not hold this subject';
    END IF;

    -- 2. Only from PURGING. The trigger enforces this too; refusing here gives
    --    a reason instead of a constraint violation.
    IF v_state <> 'PURGING' THEN
        RAISE EXCEPTION
            'terminal removal refused: lifecycle is %, not PURGING', v_state;
    END IF;

    -- 3. Every phase must report COMPLETE, read from the durable record.
    FOREACH v_phase IN ARRAY ARRAY[
        'SOURCE_DATA', 'DOCUMENTS', 'SCENARIO_RETENTION',
        'HISTORICAL_DETAIL_CLEANUP', 'AUDIT_AUTH_DEIDENTIFICATION'
    ] LOOP
        IF coalesce(identity.lifecycle_phase_status(p_user_id, v_phase), '')
           <> 'COMPLETE' THEN
            RAISE EXCEPTION
                'terminal removal refused: phase % is not COMPLETE', v_phase;
        END IF;
    END LOOP;

    -- 4. And every guard must independently read zero. A phase row is a claim;
    --    these are the evidence, and they are re-read rather than trusted.
    v_remaining := identity.count_remaining_source_data(p_user_id);
    IF v_remaining <> 0 THEN
        v_missing := format('SOURCE_DATA: %s rows', v_remaining);
    END IF;
    v_remaining := identity.count_remaining_document_privacy_work(p_user_id);
    IF v_remaining <> 0 THEN
        v_missing := coalesce(v_missing || '; ', '')
                     || format('DOCUMENTS: %s rows', v_remaining);
    END IF;
    v_remaining := identity.count_remaining_scenario_privacy_work(p_user_id);
    IF v_remaining <> 0 THEN
        v_missing := coalesce(v_missing || '; ', '')
                     || format('SCENARIO_RETENTION: %s rows', v_remaining);
    END IF;
    v_remaining := identity.count_remaining_historical_detail(p_user_id);
    IF v_remaining <> 0 THEN
        v_missing := coalesce(v_missing || '; ', '')
                     || format('HISTORICAL_DETAIL_CLEANUP: %s rows', v_remaining);
    END IF;
    v_remaining := identity.count_attributable_audit_auth(p_user_id);
    IF v_remaining <> 0 THEN
        v_missing := coalesce(v_missing || '; ', '')
                     || format('AUDIT_AUTH_DEIDENTIFICATION: %s rows', v_remaining);
    END IF;
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION
            'terminal removal refused: privacy work remains — %', v_missing;
    END IF;

    -- 5. Idempotence without lying, AND convergence after a crash.
    --
    --    The DELETE and the lifecycle UPDATE below are one transaction, so a
    --    crash between them rolls both back. But a crash between a COMMITTED
    --    removal and anything that follows must still converge, and an earlier
    --    draft returned false here and left the lifecycle in PURGING forever —
    --    the account gone, the record still saying a purge was owed. That is a
    --    wedged deletion, not idempotence.
    --
    --    So: no account means nothing to remove, which is reported honestly by
    --    returning false, but the lifecycle is still driven to its terminal
    --    state. The goal is "account gone AND deletion recorded complete", and
    --    a retry has to be able to reach it from either half.
    PERFORM 1 FROM identity.user_account WHERE id = p_user_id;
    IF NOT FOUND THEN
        UPDATE identity.account_lifecycle
           SET state = 'COMPLETE', completed_at = now(),
               claimed_by = NULL, claim_token = NULL,
               claimed_at = NULL, last_failure_code = NULL
         WHERE user_id = p_user_id AND state = 'PURGING';
        RETURN false;
    END IF;

    -- The removal itself. Thirty cascades fire into tables the phases already
    -- emptied, and four SET NULLs into columns already severed.
    DELETE FROM identity.user_account WHERE id = p_user_id;

    -- 6. And the lifecycle records what happened. PURGING -> COMPLETE is the
    --    only legal way to reach COMPLETE (41_account_lifecycle), and
    --    `ck_account_lifecycle_completion` requires COMPLETE to carry the time
    --    it happened — a state that claims to be finished without saying when
    --    is exactly the kind of half-truth this schema refuses.
    UPDATE identity.account_lifecycle
       SET state = 'COMPLETE', completed_at = now(),
           claimed_by = NULL, claim_token = NULL,
           claimed_at = NULL, last_failure_code = NULL
     WHERE user_id = p_user_id;

    RETURN true;
END;
$$;

COMMENT ON FUNCTION identity.terminal_remove_account(uuid, uuid, text) IS
    'Physically removes one account after all five privacy phases report '
    'COMPLETE and all five completion guards independently read zero. '
    'Fail-closed on six facts. Never touches audit.audit_log. Production '
    'enablement is OFF: no worker calls this.';

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER runs as the owner, so `onyx_privacy_worker` needs no DELETE
-- on `identity.user_account` — and `onyx_app_rw` keeps none, which is the
-- invariant every entry since 0060 has asserted. The capability to remove ONE
-- claimed account never becomes the capability to express "every account".
REVOKE ALL ON FUNCTION identity.terminal_remove_account(uuid, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.terminal_remove_account(uuid, uuid, text)
    TO onyx_privacy_worker;
