-- =============================================================================
-- Entry 11B6D — AUDIT_AUTH_DEIDENTIFICATION completes only when nothing
-- attributable survives (migration 0061)
-- =============================================================================
--
-- `identity.complete_lifecycle_phase` refuses to record SOURCE_DATA as done
-- while `count_remaining_source_data` is non-zero. That guarantee was written
-- for one phase and never extended, so measured on the current schema:
--
--     start_lifecycle_phase(A, AUDIT_AUTH_DEIDENTIFICATION, ...)
--     complete_lifecycle_phase(A, AUDIT_AUTH_DEIDENTIFICATION, ...)  -> true
--     count_attributable_audit_auth(A)                               -> 5
--
-- The phase could be marked COMPLETE with the account still fully attributable
-- through its login, security and consent history and its live subject
-- mapping. Since terminal account removal will eventually be gated on this
-- phase being COMPLETE, that is a status which lies in the direction that
-- matters most.
--
-- This file adds the missing branch and changes nothing else. The condition is
-- enforced in the database rather than in the worker for the reason the
-- SOURCE_DATA comment already gives: so that no caller can assert it into
-- being. A worker with a bug, or a worker replaced by a different worker, still
-- cannot record completion that is not true.
--
-- WHAT IS DELIBERATELY NOT COUNTED: retained audit rows whose `subject_key` no
-- longer resolves to an account. Those are severed historical evidence and
-- counting them would make the phase impossible to complete by design —
-- de-identification retains history, it does not delete it.
--
-- Reproduced from the committed text of 45_source_data_purge.sql with exactly
-- one added branch, so no other behaviour drifts.

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

    -- Entry 11B6D. The same rule for audit/authentication de-identification,
    -- and for the same reason: the keyhole returning successfully is not proof
    -- that nothing attributable survives. `count_attributable_audit_auth`
    -- counts ATTRIBUTION MATERIAL — live account links on login, security and
    -- consent rows, plus the `identity.account_subject` mapping itself. It
    -- deliberately does NOT count retained audit history whose subject key no
    -- longer resolves: that is severed evidence, not a violation.
    IF p_phase = 'AUDIT_AUTH_DEIDENTIFICATION' THEN
        v_remaining := identity.count_attributable_audit_auth(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'AUDIT_AUTH_DEIDENTIFICATION cannot complete: % attributable rows remain',
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
-- Which phase is this subject owed?
-- ---------------------------------------------------------------------------
-- The dispatcher has to know whether SOURCE_DATA has finished before it can
-- decide to run AUDIT_AUTH_DEIDENTIFICATION, and it must read that from the
-- durable record rather than from anything the process remembers: the worker
-- that finished the previous phase may have been a different one that has
-- since died.
--
-- It cannot read the table. `onyx_privacy_worker` holds EXECUTE on the four
-- lifecycle functions and no privilege on `identity.account_lifecycle_phase`
-- at all, which is the keyhole shape the whole worker is built on — and the
-- first version of this dispatcher tried a direct SELECT and was refused with
-- "permission denied for table account_lifecycle_phase". That refusal is
-- correct, so the question gets its own narrow verb instead of a new grant.
--
-- Reads one row, returns one closed status string, and can express nothing
-- about any other subject.
CREATE OR REPLACE FUNCTION identity.lifecycle_phase_status(
    p_user_id uuid, p_phase text
)
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
    SELECT status FROM identity.account_lifecycle_phase
     WHERE user_id = p_user_id AND phase = p_phase
$$;

ALTER FUNCTION identity.lifecycle_phase_status(uuid, text) OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION identity.lifecycle_phase_status(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.lifecycle_phase_status(uuid, text)
    TO onyx_privacy_worker;

COMMENT ON FUNCTION identity.lifecycle_phase_status(uuid, text) IS
    'Entry 11B6D. The durable status of one phase for one subject, so the privacy worker can decide which phase an account is owed without holding any privilege on identity.account_lifecycle_phase.';

-- ---------------------------------------------------------------------------
-- A de-identified login event stays de-identified
-- ---------------------------------------------------------------------------
-- Found by trying it, in the §27 resurrection test. `onyx_app_rw` holds UPDATE
-- on `identity.login_event` — correctly, because the application writes login
-- events during authentication — and nothing stopped it writing a deleted
-- account's id back onto a row the phase had already severed:
--
--     UPDATE identity.login_event SET user_id = '<deleted account>'
--      WHERE user_id IS NULL;                                    -- succeeded
--
-- `count_attributable_audit_auth` counts exactly that shape, so the account
-- would have become attributable again AFTER its phase was recorded COMPLETE.
-- No production path does this today; the point is that nothing prevented it,
-- and terminal account removal will be gated on that COMPLETE.
--
-- The guard is deliberately narrow: it refuses to change a row that has ALREADY
-- been de-identified, and says nothing about ordinary login writes. The keyhole
-- itself only ever touches rows with `deidentified_at IS NULL`, so it is
-- unaffected — de-identification remains a one-way transition performed once.

CREATE OR REPLACE FUNCTION identity.guard_deidentified_login_event()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF OLD.deidentified_at IS NOT NULL THEN
        RAISE EXCEPTION
            'identity.login_event row % is de-identified and cannot be modified',
            OLD.id
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
END;
$$;

ALTER FUNCTION identity.guard_deidentified_login_event() OWNER TO onyx_migrator;
REVOKE EXECUTE ON FUNCTION identity.guard_deidentified_login_event() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_login_event_deidentified_is_final
    ON identity.login_event;
CREATE TRIGGER trg_login_event_deidentified_is_final
    BEFORE UPDATE ON identity.login_event
    FOR EACH ROW EXECUTE FUNCTION identity.guard_deidentified_login_event();

COMMENT ON FUNCTION identity.guard_deidentified_login_event() IS
    'Entry 11B6D. De-identification of a login event is one-way: once identity.deidentify_audit_auth has severed a row, no writer may modify it. Without this the application role could write a deleted account id back onto a severed row and make the account attributable again after its phase was recorded COMPLETE.';
