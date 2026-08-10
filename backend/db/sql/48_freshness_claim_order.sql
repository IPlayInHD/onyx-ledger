-- Entry 11B5H2D — the relay must process a batch in the order it claimed it.
--
-- THE DEFECT
-- `ioe.claim_freshness_events` decides WHICH rows to claim with
-- `ORDER BY c.created_at, c.id` — oldest first, fair, with a unique tiebreak.
-- It then handed the batch back with a DIFFERENT sort:
--
--     SELECT ... FROM claimed c ORDER BY c.id;
--
-- and the relay processes the returned rows in exactly that order. So the queue
-- was drained fairly while each batch was applied in id order.
--
-- That would be harmless if id order were creation order. It is not.
-- `ref.uuid_generate_v7` builds an id from a 48-bit MILLISECOND timestamp plus
-- 74 bits of `gen_random_bytes` randomness — there is no monotonic counter — so
-- two rows created in the same millisecond sort by coin flip.
--
-- Measured on this schema, 200 trials queueing two events back to back in
-- SEPARATE transactions — the shape two real user actions have:
--
--     before   same_millisecond=82   processed_out_of_queue_order=48
--     after    same_millisecond=95   processed_out_of_queue_order=0
--
-- 48 of the 82 same-millisecond pairs came back reversed. A coin flip, and not
-- an edge case: the ordering was simply not being maintained.
--
-- (A first attempt measured this from a single `DO` block and reported every
-- pair as same-millisecond. That probe was wrong in a way worth recording:
-- `now()` is the TRANSACTION timestamp, so both rows shared one `created_at`
-- and the run said the fix changed nothing. Two events written in one
-- transaction genuinely do tie, and for them the id tiebreak is still
-- arbitrary — but they were also queued with no order to preserve. The
-- ordering that exists to be kept is between separate transactions, where
-- `created_at` differs by microseconds and never tied across 200 trials.)
--
-- WHY IT MATTERS
-- Freshness reasons are first-cause-wins, and that is a predicate, not a
-- convention: `invalidate_for_analysis` only updates rows still
-- `freshness_status = 'current'`, so whichever event is applied FIRST records
-- its reason and later ones find nothing to update. The reason is then shown to
-- a person to tell them what to do — "your figures moved" sends them to check
-- their data, "a rule changed" sends them to re-run. With the batch scrambled,
-- a user whose income edit and a rule publication landed in the same
-- millisecond got whichever reason won a coin flip rather than the one that
-- actually invalidated the advice they were looking at.
--
-- THE FIX is to return the batch in the order it was selected. `created_at` is
-- timestamptz and carries microseconds, so it orders sub-millisecond events the
-- id cannot, and `id` remains as the unique tiebreak for a genuine tie.
--
-- BASED ON 36_freshness_scope_and_reasons.sql, NOT 29. Entry P6's original
-- definition in 29 was superseded: 36 dropped and recreated this function with
-- `out_jurisdiction` in the OUT row. Replacing 29's shape here would have
-- silently reverted the jurisdiction qualifier — PostgreSQL refuses the
-- replacement outright ("cannot change return type of existing function"),
-- which is how the mistake was caught.
--
-- Only the final ORDER BY changes. The claim predicate, the attempt ceiling,
-- the stale-claim recovery, the audit writes and the ownership/privileges are
-- reproduced unchanged, because CREATE OR REPLACE rewrites the whole body.
CREATE OR REPLACE FUNCTION ioe.claim_freshness_events(
    p_batch_size integer,
    p_worker_id  text
)
RETURNS TABLE (
    out_event_id          uuid,
    out_claim_token       uuid,
    out_stale_reason_code text,
    out_analysis_id       uuid,
    out_tax_year          integer,
    out_jurisdiction      text,
    out_user_id           uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ioe, pg_catalog
AS $$
DECLARE
    v_batch integer;
    v_token uuid := ref.uuid_generate_v7();
BEGIN
    IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
        RAISE EXCEPTION 'worker_id is required to claim events';
    END IF;
    v_batch := least(greatest(coalesce(p_batch_size, 1), 1), 200);

    WITH recovered AS (
        UPDATE ioe.freshness_outbox
           SET claim_state = 'pending',
               claimed_by = NULL, claim_token = NULL, claimed_at = NULL
         WHERE claim_state = 'claimed'
           AND claimed_at < now() - ioe.freshness_claim_timeout()
        RETURNING id, claimed_by, claim_token
    )
    INSERT INTO ioe.freshness_outbox_audit
        (event_id, transition, worker_id, claim_token)
    SELECT id, 'claim_recovered', claimed_by, claim_token FROM recovered;

    RETURN QUERY
    WITH claimed AS (
        UPDATE ioe.freshness_outbox o
           SET claim_state = 'claimed',
               claimed_by = p_worker_id,
               claim_token = v_token,
               claimed_at = now(),
               attempts = o.attempts + 1
         WHERE o.id IN (
            SELECT c.id FROM ioe.freshness_outbox c
             WHERE c.claim_state = 'pending'
               AND c.attempts < 5
             ORDER BY c.created_at, c.id
             LIMIT v_batch
             FOR UPDATE SKIP LOCKED
         )
        RETURNING o.id, o.claim_token, o.stale_reason_code,
                  o.analysis_id, o.tax_year, o.jurisdiction, o.user_id,
                  o.created_at
    ), logged AS (
        INSERT INTO ioe.freshness_outbox_audit
            (event_id, transition, worker_id, claim_token)
        SELECT id, 'claimed', p_worker_id, claim_token FROM claimed
        RETURNING 1
    )
    SELECT c.id, c.claim_token, c.stale_reason_code,
           c.analysis_id, c.tax_year, c.jurisdiction, c.user_id
      FROM claimed c
     -- THE SAME ORDER THE ROWS WERE SELECTED IN. Ordering by id alone sorted
     -- same-millisecond events at random; see the header.
     ORDER BY c.created_at, c.id;
END;
$$;

COMMENT ON FUNCTION ioe.claim_freshness_events(integer, text) IS
    'Outbox workflow only: claims a bounded batch of pending events for a worker and returns them in queue order (created_at, id). Returns identifiers, codes and scope keys — never a financial value. Does not stale anything; the relay does that afterwards under ordinary RLS with app.user_id set.';
