-- =============================================================================
-- Onyx Ledger — 41 · Account deletion lifecycle  (schema: identity)
-- Alembic revision: 0047_account_lifecycle
--
-- Entry 11B1. The orchestration a privacy deletion runs through. It deletes
-- nothing: later phases do that. What it establishes is that a deletion request
-- cannot be lost, cannot be bypassed, and cannot be forged.
--
-- ACTIVE IS THE ABSENCE OF A ROW
-- There is no `ACTIVE` state stored anywhere. An account with no lifecycle row
-- is active, which means this table needs no backfill, adds nothing to the
-- write path of an ordinary account, and cannot drift out of sync with the
-- accounts it does not describe. One row per account, created when deletion is
-- requested and never duplicated — the primary key is the account.
--
-- WHY ONE TABLE
-- The obvious alternative is a request table plus a phase table plus a status
-- column on `user_account`. Three places to write means three places to be
-- inconsistent, and the interesting failure — "the account says deleting, the
-- request says complete" — becomes representable. One aggregate, one row lock,
-- one truth.
--
-- THE CUTOFF
-- `requested_at` is the authoritative instant, taken from the DATABASE clock,
-- never from a worker or an API process. Later phases compare user data against
-- it to separate "existed before the request" from "was written after it, which
-- should have been impossible".
--
-- WHAT IS DELIBERATELY NOT HERE
-- No email, no financial value, no document reference, no exception text. A
-- lifecycle row records that an account is being deleted and how far that has
-- got. Failure is a closed code (see identity.deletion_failure_code).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1 · The lifecycle aggregate
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS identity.account_lifecycle (
    user_id           uuid PRIMARY KEY
                      REFERENCES identity.user_account(id) ON DELETE CASCADE,

    state             text        NOT NULL,

    -- THE CUTOFF. Database time, set once, never moved.
    --
    -- clock_timestamp(), NOT now(). `now()` is the transaction START time, so a
    -- deletion request that waited on the lock below would be stamped with the
    -- instant it began waiting — before writes it actually waited for. The
    -- cutoff has to be the moment the request became real, because later phases
    -- use it to decide which rows a purge is responsible for, and a row dated
    -- after the cutoff is a row that purge would leave behind.
    requested_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    state_changed_at  timestamptz NOT NULL DEFAULT now(),

    -- Claim bookkeeping, mirroring ioe.freshness_outbox: a worker that dies
    -- must not strand the account, so a claim expires rather than persisting.
    claimed_by        text,
    claim_token       uuid,
    claimed_at        timestamptz,

    attempts          integer     NOT NULL DEFAULT 0,
    -- Closed code only. Entry 11A established why an exception string is
    -- privacy-sensitive; this column is one of the places that would have
    -- collected them.
    last_failure_code text,

    completed_at      timestamptz,

    -- Monotonic, bumped on every governed transition. Gives later phases a
    -- cheap optimistic-concurrency handle without a second table.
    revision          integer     NOT NULL DEFAULT 1,

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    -- The closed state set. `ACTIVE` is intentionally absent: it is the absence
    -- of the row. Adding it here would create two ways to say the same thing.
    CONSTRAINT ck_account_lifecycle_state CHECK (state IN (
        'DELETION_REQUESTED',
        'ACCESS_DISABLED',
        'PURGE_PENDING',
        'PURGING',
        'COMPLETE',
        'FAILED_RETRYABLE'
    )),

    -- A claimed row has an owner, a token and a time, or it has none of them.
    CONSTRAINT ck_account_lifecycle_claim_coherent CHECK (
        (claimed_by IS NOT NULL AND claim_token IS NOT NULL
         AND claimed_at IS NOT NULL)
        OR (claimed_by IS NULL AND claim_token IS NULL AND claimed_at IS NULL)
    ),

    -- COMPLETE is the only state that may carry a completion time, and it MUST.
    -- This is the constraint that makes "deletion was marked done before the
    -- purge ran" a database error rather than a support ticket.
    CONSTRAINT ck_account_lifecycle_completion CHECK (
        (state = 'COMPLETE' AND completed_at IS NOT NULL)
        OR (state <> 'COMPLETE' AND completed_at IS NULL)
    ),

    CONSTRAINT ck_account_lifecycle_failure_code CHECK (
        last_failure_code IS NULL
        OR last_failure_code ~ '^[A-Z][A-Z0-9_]{2,63}$'
    ),

    CONSTRAINT ck_account_lifecycle_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_account_lifecycle_revision CHECK (revision >= 1)
);

COMMENT ON TABLE identity.account_lifecycle IS
  'One row per account undergoing privacy deletion. The absence of a row means the account is active. requested_at is the authoritative deletion cutoff, taken from the database clock.';
COMMENT ON COLUMN identity.account_lifecycle.requested_at IS
  'The deletion cutoff. Later purge phases compare user data against this instant to separate data that existed before the request from data written after it.';
COMMENT ON COLUMN identity.account_lifecycle.last_failure_code IS
  'Closed failure code, never an exception message. A lifecycle row must not become a place raw errors accumulate.';
COMMENT ON COLUMN identity.account_lifecycle.state IS
  'Closed lifecycle state. ACTIVE is deliberately not a value: it is the absence of the row.';

-- The worker's claim scan: unclaimed rows in states that still need work.
CREATE INDEX IF NOT EXISTS ix_account_lifecycle_claimable
    ON identity.account_lifecycle (requested_at)
    WHERE claimed_by IS NULL
      AND state IN ('DELETION_REQUESTED', 'ACCESS_DISABLED', 'FAILED_RETRYABLE');

-- Abandoned-claim recovery scans only currently-claimed rows.
CREATE INDEX IF NOT EXISTS ix_account_lifecycle_claimed
    ON identity.account_lifecycle (claimed_at)
    WHERE claimed_by IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 1a · Ordering the cutoff against writes already in flight
--
-- THE RACE. A write and a deletion request arrive together. The write's session
-- reads the lifecycle state, sees nothing, and proceeds. The deletion request
-- commits. The write commits AFTER it. The row is now dated later than the
-- cutoff, and a purge phase bounded on `requested_at` would never claim it —
-- so an account reported as deleted would still have data.
--
-- Refusing at the start of the request does not fix this: the check is correct
-- when it runs and stale by the time the write lands. The two transactions have
-- to be ordered against each other, and a transaction-scoped advisory lock is
-- the cheapest way to say so:
--
--   every user-bound request  takes it SHARED    (they do not conflict)
--   a deletion request        takes it EXCLUSIVE (it conflicts with all of them)
--
-- Whichever wins, the outcome is truthful. If writes hold it, the deletion
-- waits and is stamped after they land. If the deletion holds it, the write
-- waits, then reads the row it was waiting for and is refused. There is no
-- interleaving left where a write commits after a cutoff that precedes it.
--
-- Advisory rather than a row lock on `user_account`: `last_login_at` is updated
-- on that row, so a shared lock held for the length of every authenticated
-- request would put logins behind unrelated work.
CREATE OR REPLACE FUNCTION identity.lifecycle_lock_key(p_user_id uuid)
RETURNS bigint
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog
AS $$
    -- Domain-separated so this can never collide with another advisory-lock
    -- user in this database that happens to hash the same account id.
    SELECT hashtextextended('onyx.account_lifecycle:' || p_user_id::text, 0)
$$;

COMMENT ON FUNCTION identity.lifecycle_lock_key(uuid) IS
  'Advisory-lock key ordering user writes against a deletion request for the same account. Shared for ordinary work, exclusive for the request.';

REVOKE ALL ON FUNCTION identity.lifecycle_lock_key(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.lifecycle_lock_key(uuid)
    TO onyx_app_rw, onyx_app_ro;

-- ---------------------------------------------------------------------------
-- 2 · Governed transitions, enforced by the database
--
-- The service enforces these too. Both, deliberately: the service is the API
-- everything should use, and the trigger is what makes "everything" true — a
-- migration, a psql session or a future repository method cannot walk an
-- account from DELETION_REQUESTED straight to COMPLETE.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION identity.enforce_lifecycle_transition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = identity, pg_catalog
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- A lifecycle may only begin at its beginning.
        IF NEW.state <> 'DELETION_REQUESTED' THEN
            RAISE EXCEPTION
                'account lifecycle must start at DELETION_REQUESTED, not %',
                NEW.state
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- The cutoff is immutable. Moving it would let a later phase mistake
    -- post-request writes for pre-request ones.
    IF NEW.requested_at IS DISTINCT FROM OLD.requested_at THEN
        RAISE EXCEPTION 'account lifecycle requested_at is immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
        RAISE EXCEPTION 'account lifecycle user_id is immutable'
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.state = OLD.state THEN
        RETURN NEW;   -- claim/attempt bookkeeping, not a transition
    END IF;

    IF NOT (
           (OLD.state = 'DELETION_REQUESTED' AND NEW.state IN
                ('ACCESS_DISABLED', 'FAILED_RETRYABLE'))
        OR (OLD.state = 'ACCESS_DISABLED'    AND NEW.state IN
                ('PURGE_PENDING', 'FAILED_RETRYABLE'))
        OR (OLD.state = 'PURGE_PENDING'      AND NEW.state IN
                ('PURGING', 'FAILED_RETRYABLE'))
        OR (OLD.state = 'PURGING'            AND NEW.state IN
                ('COMPLETE', 'FAILED_RETRYABLE'))
        -- Recovery returns to the phase that failed, never forward.
        OR (OLD.state = 'FAILED_RETRYABLE'   AND NEW.state IN
                ('DELETION_REQUESTED', 'ACCESS_DISABLED', 'PURGE_PENDING',
                 'PURGING'))
    ) THEN
        RAISE EXCEPTION 'illegal account lifecycle transition % -> %',
            OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;

    NEW.state_changed_at := now();
    NEW.revision := OLD.revision + 1;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION identity.enforce_lifecycle_transition() IS
  'Rejects illegal lifecycle transitions and freezes the deletion cutoff. COMPLETE is reachable only from PURGING, so an account cannot be reported deleted before a purge phase runs.';

DROP TRIGGER IF EXISTS trg_account_lifecycle_transition
    ON identity.account_lifecycle;
CREATE TRIGGER trg_account_lifecycle_transition
    BEFORE INSERT OR UPDATE ON identity.account_lifecycle
    FOR EACH ROW EXECUTE FUNCTION identity.enforce_lifecycle_transition();

-- Deletion of a lifecycle row would erase the evidence that a deletion was
-- requested — including for a restored backup, which needs exactly that record
-- to know what to re-delete. Only the account's own removal takes it away.
CREATE OR REPLACE FUNCTION identity.reject_lifecycle_delete()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'account lifecycle rows are not deletable'
        USING ERRCODE = 'check_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_account_lifecycle_no_delete
    ON identity.account_lifecycle;
CREATE TRIGGER trg_account_lifecycle_no_delete
    BEFORE DELETE ON identity.account_lifecycle
    FOR EACH ROW EXECUTE FUNCTION identity.reject_lifecycle_delete();

-- ---------------------------------------------------------------------------
-- 3 · Append-only transition log
--
-- Bounded codes and identifiers. This is the record a restored backup replays
-- and an auditor reads; it must survive the lifecycle row's own churn.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS identity.account_lifecycle_event (
    id            uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id       uuid NOT NULL,
    event_code    text NOT NULL CHECK (event_code IN (
                      'DELETION_REQUESTED',
                      'ACCESS_DISABLED',
                      'SESSIONS_REVOKED',
                      'PHASE_CLAIMED',
                      'PHASE_COMPLETED',
                      'PHASE_FAILED',
                      'CLAIM_RECOVERED',
                      'LIFECYCLE_COMPLETED')),
    from_state    text,
    to_state      text,
    worker_id     text,
    reason_code   text CHECK (reason_code IS NULL
                              OR reason_code ~ '^[A-Z][A-Z0-9_]{2,63}$'),
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE identity.account_lifecycle_event IS
  'Append-only lifecycle transitions. Closed codes and identifiers only — never an email address, a financial value, a document reference or an exception message.';

CREATE INDEX IF NOT EXISTS ix_account_lifecycle_event_user
    ON identity.account_lifecycle_event (user_id, created_at);

-- No user FK on purpose: this record must outlive the account it describes, so
-- a restored backup can be told which accounts were deleted after the snapshot.
-- The Entry 11A specification requires exactly that (§16, restore invariant).

CREATE OR REPLACE FUNCTION identity.reject_lifecycle_event_mutation()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'identity.account_lifecycle_event is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_account_lifecycle_event_immutable
    ON identity.account_lifecycle_event;
CREATE TRIGGER trg_account_lifecycle_event_immutable
    BEFORE UPDATE OR DELETE ON identity.account_lifecycle_event
    FOR EACH ROW EXECUTE FUNCTION identity.reject_lifecycle_event_mutation();

-- ---------------------------------------------------------------------------
-- 4 · Row-level security
--
-- A user may see that their own deletion is under way and may start one. They
-- may not advance it, may not mark it complete, and may not see anybody else's.
-- There is no UPDATE policy and no DELETE policy at all, so an ordinary session
-- cannot forge progress even with a working `app.user_id` — the absence of the
-- policy is the control, not a `WITH CHECK` that has to be got right.
--
-- The worker does NOT go through RLS: it uses the privileged claim functions in
-- section 5, exactly as the freshness relay does.
-- ---------------------------------------------------------------------------
ALTER TABLE identity.account_lifecycle ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity.account_lifecycle FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_self_account_lifecycle_select ON identity.account_lifecycle;
CREATE POLICY p_self_account_lifecycle_select ON identity.account_lifecycle
    FOR SELECT
    USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid);

DROP POLICY IF EXISTS p_self_account_lifecycle_insert ON identity.account_lifecycle;
CREATE POLICY p_self_account_lifecycle_insert ON identity.account_lifecycle
    FOR INSERT
    WITH CHECK (user_id = nullif(current_setting('app.user_id', true), '')::uuid);

ALTER TABLE identity.account_lifecycle_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity.account_lifecycle_event FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_self_account_lifecycle_event_select
    ON identity.account_lifecycle_event;
CREATE POLICY p_self_account_lifecycle_event_select
    ON identity.account_lifecycle_event
    FOR SELECT
    USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid);

DROP POLICY IF EXISTS p_self_account_lifecycle_event_insert
    ON identity.account_lifecycle_event;
CREATE POLICY p_self_account_lifecycle_event_insert
    ON identity.account_lifecycle_event
    FOR INSERT
    WITH CHECK (user_id = nullif(current_setting('app.user_id', true), '')::uuid);

-- THE REVOKE IS THE CONTROL, NOT THE ABSENCE OF A GRANT.
--
-- `16_rls_grants.sql` sets ALTER DEFAULT PRIVILEGES IN SCHEMA identity ...
-- GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO onyx_app_rw, so every table
-- the migrator creates in this schema arrives with UPDATE and DELETE already
-- granted. Writing only `GRANT SELECT, INSERT` here would therefore have been a
-- no-op that read like a restriction — the runtime role would have held UPDATE
-- on the lifecycle row, and "ordinary roles cannot forge progress" would have
-- rested entirely on the absence of an RLS UPDATE policy, which fails SILENTLY
-- (zero rows affected, no error) rather than loudly.
--
-- So the privileges are revoked explicitly and then re-granted narrowly. A
-- forged advance now raises `permission denied`, which a test can see and an
-- operator can find in a log.
REVOKE ALL ON identity.account_lifecycle FROM PUBLIC;
REVOKE ALL ON identity.account_lifecycle_event FROM PUBLIC;
REVOKE ALL ON identity.account_lifecycle FROM onyx_app_rw, onyx_app_ro;
REVOKE ALL ON identity.account_lifecycle_event FROM onyx_app_rw, onyx_app_ro;

GRANT SELECT, INSERT ON identity.account_lifecycle TO onyx_app_rw;
-- Append-only: INSERT and SELECT, never UPDATE or DELETE. The immutability
-- trigger says the same thing; the grant means the attempt never reaches it.
GRANT SELECT, INSERT ON identity.account_lifecycle_event TO onyx_app_rw;
GRANT SELECT ON identity.account_lifecycle TO onyx_app_ro;
GRANT SELECT ON identity.account_lifecycle_event TO onyx_app_ro;

-- ---------------------------------------------------------------------------
-- 5 · The privileged lifecycle worker interface
--
-- The deletion worker is a CROSS-TENANT system process: it must claim an
-- account it is not, which no tenant session can do. Same shape as the
-- freshness relay's keyhole, and same reasoning — RLS is not disabled, the
-- worker gets no table grants, and the privileged surface is three functions
-- that move one row between enumerated states.
--
-- Every function pins `search_path`. None takes a table name, a filter or a SQL
-- fragment. None returns an email address or any financial value: a claim
-- returns the account id, the state and the cutoff, which is the minimum a
-- purge phase needs to do its job.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_privacy_worker') THEN
        CREATE ROLE onyx_privacy_worker NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA identity TO onyx_privacy_worker;

-- How long a claim may be held before another worker may take it. A worker that
-- dies mid-phase must not strand the account forever.
CREATE OR REPLACE FUNCTION identity.lifecycle_claim_timeout()
RETURNS interval LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog
AS $$ SELECT interval '10 minutes' $$;

CREATE OR REPLACE FUNCTION identity.claim_account_lifecycle(
    p_batch_size integer,
    p_worker_id  text
)
RETURNS TABLE (
    out_user_id      uuid,
    out_state        text,
    out_requested_at timestamptz,
    out_claim_token  uuid,
    out_revision     integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
DECLARE
    v_batch integer;
    v_token uuid := ref.uuid_generate_v7();
BEGIN
    IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
        RAISE EXCEPTION 'worker_id is required to claim a lifecycle';
    END IF;
    v_batch := least(greatest(coalesce(p_batch_size, 1), 1), 50);

    -- Recovery first: a claim older than the timeout is released so the account
    -- is not stranded by a worker that disappeared.
    WITH recovered AS (
        UPDATE identity.account_lifecycle
           SET claimed_by = NULL, claim_token = NULL, claimed_at = NULL
         WHERE claimed_by IS NOT NULL
           AND claimed_at < now() - identity.lifecycle_claim_timeout()
        RETURNING user_id, claimed_by AS prior_worker
    )
    INSERT INTO identity.account_lifecycle_event
        (user_id, event_code, worker_id, reason_code)
    SELECT user_id, 'CLAIM_RECOVERED', prior_worker, 'CLAIM_EXPIRED'
      FROM recovered;

    RETURN QUERY
    WITH claimable AS (
        SELECT l.user_id
          FROM identity.account_lifecycle l
         WHERE l.claimed_by IS NULL
           AND l.state IN ('DELETION_REQUESTED', 'ACCESS_DISABLED',
                           'FAILED_RETRYABLE')
         ORDER BY l.requested_at
         FOR UPDATE SKIP LOCKED
         LIMIT v_batch
    ), claimed AS (
        UPDATE identity.account_lifecycle l
           SET claimed_by = p_worker_id,
               claim_token = v_token,
               claimed_at = now(),
               attempts = l.attempts + 1,
               updated_at = now()
          FROM claimable c
         WHERE l.user_id = c.user_id
        RETURNING l.user_id, l.state, l.requested_at, l.claim_token, l.revision
    ), logged AS (
        INSERT INTO identity.account_lifecycle_event
            (user_id, event_code, from_state, to_state, worker_id)
        SELECT user_id, 'PHASE_CLAIMED', state, state, p_worker_id
          FROM claimed
        RETURNING 1
    )
    SELECT user_id, state, requested_at, claim_token, revision FROM claimed;
END;
$$;

CREATE OR REPLACE FUNCTION identity.advance_account_lifecycle(
    p_user_id     uuid,
    p_claim_token uuid,
    p_next_state  text,
    p_worker_id   text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
DECLARE
    v_from text;
BEGIN
    -- The claim token is the authority. A worker whose claim expired and was
    -- taken by another cannot advance the account it no longer holds.
    SELECT state INTO v_from
      FROM identity.account_lifecycle
     WHERE user_id = p_user_id AND claim_token = p_claim_token
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    UPDATE identity.account_lifecycle
       SET state = p_next_state,
           claimed_by = NULL, claim_token = NULL, claimed_at = NULL,
           last_failure_code = NULL,
           completed_at = CASE WHEN p_next_state = 'COMPLETE'
                               THEN now() ELSE NULL END,
           updated_at = now()
     WHERE user_id = p_user_id;

    INSERT INTO identity.account_lifecycle_event
        (user_id, event_code, from_state, to_state, worker_id)
    VALUES (p_user_id,
            CASE WHEN p_next_state = 'COMPLETE'
                 THEN 'LIFECYCLE_COMPLETED' ELSE 'PHASE_COMPLETED' END,
            v_from, p_next_state, p_worker_id);
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION identity.fail_account_lifecycle(
    p_user_id     uuid,
    p_claim_token uuid,
    p_reason_code text,
    p_worker_id   text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
DECLARE
    v_from text;
BEGIN
    IF p_reason_code !~ '^[A-Z][A-Z0-9_]{2,63}$' THEN
        RAISE EXCEPTION 'lifecycle failure reason must be a closed code';
    END IF;

    SELECT state INTO v_from
      FROM identity.account_lifecycle
     WHERE user_id = p_user_id AND claim_token = p_claim_token
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    UPDATE identity.account_lifecycle
       SET state = 'FAILED_RETRYABLE',
           last_failure_code = p_reason_code,
           claimed_by = NULL, claim_token = NULL, claimed_at = NULL,
           updated_at = now()
     WHERE user_id = p_user_id;

    INSERT INTO identity.account_lifecycle_event
        (user_id, event_code, from_state, to_state, worker_id, reason_code)
    VALUES (p_user_id, 'PHASE_FAILED', v_from, 'FAILED_RETRYABLE',
            p_worker_id, p_reason_code);
    RETURN true;
END;
$$;

-- ---- the authentication cutoff read ----------------------------------------
-- Login runs in an ANONYMOUS session: `app.user_id` is unset by design, so the
-- RLS policy above makes the lifecycle row invisible — and an authentication
-- check that silently sees nothing would admit every deleting account. This is
-- the narrowest possible way to ask the one question login needs answered.
--
-- It takes an account id and returns a state or NULL. No email, no timestamp,
-- no claim, no failure code. The application role can already read
-- `identity.user_account` in full, so this discloses nothing it did not have.
CREATE OR REPLACE FUNCTION identity.account_deletion_state(p_user_id uuid)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
    SELECT state FROM identity.account_lifecycle WHERE user_id = p_user_id
$$;

COMMENT ON FUNCTION identity.account_deletion_state(uuid) IS
  'Deletion state for one account, or NULL if active. Exists because login is an anonymous session that RLS correctly hides the lifecycle row from. Returns a closed state code and nothing else.';

REVOKE ALL ON FUNCTION identity.account_deletion_state(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.account_deletion_state(uuid) TO onyx_app_rw;

REVOKE ALL ON FUNCTION identity.claim_account_lifecycle(integer, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.advance_account_lifecycle(uuid, uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.fail_account_lifecycle(uuid, uuid, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION identity.claim_account_lifecycle(integer, text)
    TO onyx_privacy_worker;
GRANT EXECUTE ON FUNCTION identity.advance_account_lifecycle(uuid, uuid, text, text)
    TO onyx_privacy_worker;
GRANT EXECUTE ON FUNCTION identity.fail_account_lifecycle(uuid, uuid, text, text)
    TO onyx_privacy_worker;

-- The application role runs the worker in this deployment shape, so it needs
-- the same EXECUTE. It still has no UPDATE privilege on the table: the ONLY way
-- any role can advance a lifecycle is through these three functions.
GRANT EXECUTE ON FUNCTION identity.claim_account_lifecycle(integer, text)
    TO onyx_app_rw;
GRANT EXECUTE ON FUNCTION identity.advance_account_lifecycle(uuid, uuid, text, text)
    TO onyx_app_rw;
GRANT EXECUTE ON FUNCTION identity.fail_account_lifecycle(uuid, uuid, text, text)
    TO onyx_app_rw;
