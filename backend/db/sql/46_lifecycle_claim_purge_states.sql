-- Entry 11B5E — make the purge states claimable.
--
-- THE PHASE COULD NEVER BE PICKED UP. `identity.claim_account_lifecycle` only
-- ever returned accounts in DELETION_REQUESTED, ACCESS_DISABLED or
-- FAILED_RETRYABLE, and the partial index backing it carried the same three.
-- That was coherent while it lasted: Entry 11B2 declared PURGE_PENDING its
-- terminal state and nothing existed to claim past it. Entry 11B5C then built
-- the source-purge keyhole and Entry 11B5E the worker that calls it, and the
-- worker's first run failed with
--
--     AssertionError: could not claim to reach PURGING
--
-- because an account that reached PURGE_PENDING was invisible to every claim.
--
-- PURGING IS INCLUDED, NOT ONLY PURGE_PENDING. A worker that dies mid-phase
-- leaves the account in PURGING with an expired lease; without PURGING here
-- nothing could ever reclaim it, and the subject would be stranded in exactly
-- the state the phase table exists to make recoverable.
--
-- The lease-recovery half of the function is unchanged, so a claim released by
-- timeout re-enters the queue the same way it always did.

DROP INDEX IF EXISTS identity.ix_account_lifecycle_claimable;
CREATE INDEX IF NOT EXISTS ix_account_lifecycle_claimable
    ON identity.account_lifecycle (requested_at)
 WHERE claimed_by IS NULL
   AND state IN ('DELETION_REQUESTED', 'ACCESS_DISABLED',
                 'PURGE_PENDING', 'PURGING', 'FAILED_RETRYABLE');

CREATE OR REPLACE FUNCTION identity.claim_account_lifecycle(p_batch_size integer, p_worker_id text)
 RETURNS TABLE(out_user_id uuid, out_state text, out_requested_at timestamp with time zone, out_claim_token uuid, out_revision integer)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'identity', 'pg_catalog'
AS $function$
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
                           'PURGE_PENDING', 'PURGING', 'FAILED_RETRYABLE')
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
$function$

;
