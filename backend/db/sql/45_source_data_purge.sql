-- Entry 11B5 — durable per-phase progress, and the source-data purge keyhole.
--
-- WHY A PHASE TABLE AND NOT MORE STATES
-- `identity.account_lifecycle.state` is one scalar. Account privacy deletion is
-- several phases — source data, documents, audit/authentication
-- de-identification, and more later — and a single scalar cannot say "expenses
-- are gone, income is not". A worker that crashed between them would restart
-- with no way to tell which half it had done, and the honest options are either
-- to redo everything (wrong once a phase is not idempotent) or to guess.
--
-- Adding SOURCE_DATA_PURGING, DOCUMENTS_PURGING, ... to the top-level state
-- machine was the other option and is worse: every future phase widens the
-- CHECK constraint, the transition trigger, and every reader of `state`. The
-- account stays in PURGING; a row per phase carries the progress.
--
-- WHY THE WORKER GETS A FUNCTION AND NOT `DELETE`
-- Granting `onyx_privacy_worker` DELETE on the finance, profile and wealth
-- tables would give it the power to empty those tables for EVERY tenant, and
-- the only thing standing between that grant and a catastrophe would be the
-- correctness of the WHERE clause in application code. The keyhole shape used
-- everywhere else in this schema applies: one SECURITY DEFINER function that
-- takes ONE subject, is authorised by the claim token, and cannot express
-- "every user".
--
-- AND IT RUNS AS THE TENANT
-- The function sets `app.user_id` to the subject for the duration of the
-- transaction, so the deletes execute under the SAME row-level security every
-- ordinary request runs under. Cross-tenant deletion is not prevented by the
-- WHERE clause being right; it is prevented by RLS refusing to show the
-- function anything else. Entry 11B4's freshness relay reached the same
-- conclusion: "nothing here is privileged".

-- ---------------------------------------------------------------------------
-- Durable per-phase progress
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS identity.account_lifecycle_phase (
    user_id           uuid        NOT NULL,
    phase             text        NOT NULL,
    status            text        NOT NULL DEFAULT 'PENDING',
    attempts          integer     NOT NULL DEFAULT 0,
    last_failure_code text,
    started_at        timestamptz,
    completed_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT account_lifecycle_phase_pkey PRIMARY KEY (user_id, phase),
    CONSTRAINT ck_lifecycle_phase_name CHECK (
        phase IN ('SOURCE_DATA', 'DOCUMENTS', 'AUDIT_AUTH_DEIDENTIFICATION')),
    CONSTRAINT ck_lifecycle_phase_status CHECK (
        status IN ('PENDING', 'RUNNING', 'COMPLETE', 'FAILED_RETRYABLE')),
    CONSTRAINT ck_lifecycle_phase_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_lifecycle_phase_completion CHECK (
        (status = 'COMPLETE' AND completed_at IS NOT NULL)
     OR (status <> 'COMPLETE' AND completed_at IS NULL)),
    CONSTRAINT ck_lifecycle_phase_failure_code CHECK (
        last_failure_code IS NULL
        OR last_failure_code ~ '^[A-Z][A-Z0-9_]{2,63}$')
);

-- NO FOREIGN KEY TO identity.user_account, and that is deliberate — PD-9.
-- A purge phase ends by removing the account row, and the phases after it still
-- have work to do. The FK to `account_lifecycle` is safe because Entry 11B3
-- made THAT record outlive the account; pointing at `user_account` instead
-- would reintroduce exactly the dependency PD-9 removed.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_lifecycle_phase_subject'
    ) THEN
        ALTER TABLE identity.account_lifecycle_phase
            ADD CONSTRAINT fk_lifecycle_phase_subject
            FOREIGN KEY (user_id) REFERENCES identity.account_lifecycle(user_id)
            ON DELETE CASCADE;
    END IF;
END
$$;

COMMENT ON TABLE identity.account_lifecycle_phase IS
    'Durable per-phase progress for one account deletion. The account stays in '
    'PURGING; this says which phase is pending, running, done or owed a retry.';

-- A phase record is progress, not user data, and losing one silently would let
-- a half-finished purge look finished. Same protection the ledger has.
CREATE OR REPLACE FUNCTION identity.reject_lifecycle_phase_delete()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = identity, pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'account_lifecycle_phase rows are not deletable';
END;
$$;

DROP TRIGGER IF EXISTS trg_lifecycle_phase_no_delete
    ON identity.account_lifecycle_phase;
CREATE TRIGGER trg_lifecycle_phase_no_delete
    BEFORE DELETE ON identity.account_lifecycle_phase
    FOR EACH ROW EXECUTE FUNCTION identity.reject_lifecycle_phase_delete();

ALTER TABLE identity.account_lifecycle_phase ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity.account_lifecycle_phase FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_self_account_lifecycle_phase_select
    ON identity.account_lifecycle_phase;
CREATE POLICY p_self_account_lifecycle_phase_select
    ON identity.account_lifecycle_phase FOR SELECT
    USING (user_id = ref.current_app_user());

-- The application may READ its own progress and may never write it. Progress is
-- written by the worker through the definer functions below, which is what
-- makes "the phase says COMPLETE" mean something.
REVOKE ALL ON identity.account_lifecycle_phase FROM PUBLIC;
REVOKE ALL ON identity.account_lifecycle_phase FROM onyx_app_rw, onyx_app_ro;
GRANT SELECT ON identity.account_lifecycle_phase TO onyx_app_rw, onyx_app_ro;

GRANT USAGE ON SCHEMA identity TO onyx_privacy_worker;

-- ---------------------------------------------------------------------------
-- The purge keyhole
-- ---------------------------------------------------------------------------
-- One subject, authorised by the claim token, executed under the subject's own
-- row-level security. Returns COUNTS — never a value, never a row.
CREATE OR REPLACE FUNCTION identity.purge_source_data(
    p_user_id     uuid,
    p_claim_token uuid,
    p_worker_id   text
)
RETURNS TABLE (out_table text, out_deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, finance, profile, wealth, ref, pg_catalog
AS $$
DECLARE
    v_prior text;
    v_n     bigint;
BEGIN
    -- The claim token is the authority, exactly as in advance_account_lifecycle.
    -- A worker whose claim expired and was taken by another cannot purge the
    -- account it no longer holds.
    PERFORM 1 FROM identity.account_lifecycle
      WHERE user_id = p_user_id AND claim_token = p_claim_token
      FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'purge refused: the claim token does not hold this subject';
    END IF;

    -- Run as the tenant. The deletes below are confined by the same policies an
    -- ordinary request runs under, so a WHERE clause that forgot `user_id`
    -- still could not reach another tenant.
    v_prior := current_setting('app.user_id', true);
    PERFORM set_config('app.user_id', p_user_id::text, true);

    -- Set-based, one statement per table. The partitioned parents are named and
    -- PostgreSQL routes to every partition; naming the partitions would give
    -- this a hard-coded year list that a 2026 partition silently escapes.
    DELETE FROM finance.income_source  WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'finance.income_source';  out_deleted := v_n; RETURN NEXT;

    DELETE FROM finance.expense_record WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'finance.expense_record'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM profile.dependent      WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'profile.dependent';      out_deleted := v_n; RETURN NEXT;

    DELETE FROM profile.spouse_profile WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'profile.spouse_profile'; out_deleted := v_n; RETURN NEXT;

    DELETE FROM profile.tax_profile    WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'profile.tax_profile';    out_deleted := v_n; RETURN NEXT;

    DELETE FROM profile.user_profile   WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'profile.user_profile';   out_deleted := v_n; RETURN NEXT;

    -- wealth children (valuation, balance, registered detail) go by cascade.
    DELETE FROM wealth.asset           WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'wealth.asset';           out_deleted := v_n; RETURN NEXT;

    DELETE FROM wealth.liability       WHERE user_id = p_user_id;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    out_table := 'wealth.liability';       out_deleted := v_n; RETURN NEXT;

    PERFORM set_config('app.user_id', coalesce(v_prior, ''), true);
    RETURN;
END;
$$;

-- The authoritative completeness check. COUNTS ONLY: the thing that verifies a
-- privacy deletion must not become a place personal data is read out to. The
-- phase may not be marked COMPLETE on the strength of statements executed —
-- only on the strength of this returning zero.
CREATE OR REPLACE FUNCTION identity.count_remaining_source_data(p_user_id uuid)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, finance, profile, wealth, ref, pg_catalog
AS $$
DECLARE
    v_prior text;
    v_total bigint;
BEGIN
    v_prior := current_setting('app.user_id', true);
    PERFORM set_config('app.user_id', p_user_id::text, true);

    SELECT (SELECT count(*) FROM finance.income_source  WHERE user_id = p_user_id)
         + (SELECT count(*) FROM finance.expense_record WHERE user_id = p_user_id)
         + (SELECT count(*) FROM profile.dependent      WHERE user_id = p_user_id)
         + (SELECT count(*) FROM profile.spouse_profile WHERE user_id = p_user_id)
         + (SELECT count(*) FROM profile.tax_profile    WHERE user_id = p_user_id)
         + (SELECT count(*) FROM profile.user_profile   WHERE user_id = p_user_id)
         + (SELECT count(*) FROM wealth.asset           WHERE user_id = p_user_id)
         + (SELECT count(*) FROM wealth.liability       WHERE user_id = p_user_id)
      INTO v_total;

    PERFORM set_config('app.user_id', coalesce(v_prior, ''), true);
    RETURN v_total;
END;
$$;

-- Phase progress, written only here.
CREATE OR REPLACE FUNCTION identity.start_lifecycle_phase(
    p_user_id uuid, p_phase text, p_claim_token uuid, p_worker_id text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
BEGIN
    PERFORM 1 FROM identity.account_lifecycle
      WHERE user_id = p_user_id AND claim_token = p_claim_token FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    INSERT INTO identity.account_lifecycle_phase
        (user_id, phase, status, attempts, started_at)
    VALUES (p_user_id, p_phase, 'RUNNING', 1, now())
    ON CONFLICT (user_id, phase) DO UPDATE
        SET status = 'RUNNING',
            attempts = identity.account_lifecycle_phase.attempts + 1,
            started_at = now(),
            last_failure_code = NULL,
            updated_at = now()
      WHERE identity.account_lifecycle_phase.status <> 'COMPLETE';
    RETURN true;
END;
$$;

-- Completion is REFUSED while anything in scope remains. This is the guarantee
-- the phase exists to make, and it is enforced here rather than in application
-- code so that no caller can assert it into being.
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

    UPDATE identity.account_lifecycle_phase
       SET status = 'COMPLETE', completed_at = now(),
           last_failure_code = NULL, updated_at = now()
     WHERE user_id = p_user_id AND phase = p_phase;
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION identity.fail_lifecycle_phase(
    p_user_id uuid, p_phase text, p_claim_token uuid,
    p_reason_code text, p_worker_id text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
BEGIN
    IF p_reason_code !~ '^[A-Z][A-Z0-9_]{2,63}$' THEN
        RAISE EXCEPTION 'phase failure reason must be a closed code';
    END IF;
    PERFORM 1 FROM identity.account_lifecycle
      WHERE user_id = p_user_id AND claim_token = p_claim_token FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    UPDATE identity.account_lifecycle_phase
       SET status = 'FAILED_RETRYABLE', last_failure_code = p_reason_code,
           updated_at = now()
     WHERE user_id = p_user_id AND phase = p_phase;
    RETURN FOUND;
END;
$$;

-- PUBLIC gets nothing; the worker gets exactly these four verbs and no DELETE
-- on any tenant table.
REVOKE ALL ON FUNCTION identity.purge_source_data(uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.count_remaining_source_data(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.start_lifecycle_phase(uuid, text, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.complete_lifecycle_phase(uuid, text, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.fail_lifecycle_phase(uuid, text, uuid, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION identity.purge_source_data(uuid, uuid, text)
    TO onyx_privacy_worker;
GRANT EXECUTE ON FUNCTION identity.count_remaining_source_data(uuid)
    TO onyx_privacy_worker;
GRANT EXECUTE ON FUNCTION identity.start_lifecycle_phase(uuid, text, uuid, text)
    TO onyx_privacy_worker;
GRANT EXECUTE ON FUNCTION identity.complete_lifecycle_phase(uuid, text, uuid, text)
    TO onyx_privacy_worker;
GRANT EXECUTE ON FUNCTION identity.fail_lifecycle_phase(uuid, text, uuid, text, text)
    TO onyx_privacy_worker;
