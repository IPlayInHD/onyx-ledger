-- =============================================================================
-- Entry 11B6C — evidence-safe retained-root detachment (migration 0060)
-- =============================================================================
--
-- Four tables hold evidence proven to require survival of an account deletion,
-- and all four reference `identity.user_account` with ON DELETE CASCADE:
--
--     analysis.analysis_run        REPLAY_REQUIRED_RETAIN
--     ioe.optimization_run         REPLAY_REQUIRED_RETAIN
--     ioe.integrity_check          SECURITY_EVIDENCE_RETAIN
--     ioe.scenario                 DEIDENTIFY_THEN_RETAIN (row-state dependent)
--
-- The cascade cannot fire today: it reaches `ioe.integrity_check`, whose
-- append-only guard rejects DELETE with no escape, so
-- `DELETE FROM identity.user_account` raises. Account deletion is therefore not
-- currently destructive — it is impossible. This file makes it possible without
-- making it destructive.
--
-- WHAT THIS DOES: drops the four foreign keys and nothing else. `user_id` keeps
-- its value, its NOT NULL, and its indexes. No row is updated. No row is
-- deleted. That is the entire point of the model — every immutability and
-- append-only trigger stays armed, because nothing needs to be rewritten.
--
-- WHY NOT `SET NULL`: `user_id` is NOT NULL on all four, so the cascade would
-- raise rather than degrade; and `ioe.scenario` seals `user_id` once completed
-- while `ioe.integrity_check` refuses UPDATE once terminal.
--
-- WHY NOT `NO ACTION` or `RESTRICT`: neither is severance. They convert an
-- automatic child delete into a REFUSED PARENT DELETE, which is the same dead
-- end the append-only guard already produces. A retained row whose account is
-- gone cannot satisfy any normal foreign key, so the constraint has to go.
--
-- WHY NOT a subject-key rewrite: it would require UPDATEing sealed evidence,
-- and the measured answer is that two of the four refuse it outright.
--
-- WHAT `user_id` MEANS AFTERWARDS: a HISTORICAL SUBJECT CORRELATOR. It still
-- identifies whose evidence a row is, and RLS still resolves ownership through
-- it — `ref.current_app_user()` reads a setting and never consults
-- `identity.user_account`, so removing the account row neither grants nor
-- withdraws access. It is no longer a guarantee that the account exists.

-- ---------------------------------------------------------------------------
-- 1. Detach the four retained roots
-- ---------------------------------------------------------------------------
-- Catalog-only changes: no table scan, no row rewrite. Each takes an
-- ACCESS EXCLUSIVE lock on the child and on identity.user_account for the
-- duration of the statement.

ALTER TABLE analysis.analysis_run
    DROP CONSTRAINT IF EXISTS analysis_run_user_id_fkey;

ALTER TABLE ioe.optimization_run
    DROP CONSTRAINT IF EXISTS optimization_run_user_id_fkey;

ALTER TABLE ioe.scenario
    DROP CONSTRAINT IF EXISTS scenario_user_id_fkey;

ALTER TABLE ioe.integrity_check
    DROP CONSTRAINT IF EXISTS integrity_check_user_id_fkey;

COMMENT ON COLUMN analysis.analysis_run.user_id IS
    'Entry 11B6C. HISTORICAL SUBJECT CORRELATOR, not a live foreign key. The account row it names may have been deleted; the value is never rewritten. RLS resolves ownership through it via ref.current_app_user(), which reads a setting and never consults identity.user_account.';
COMMENT ON COLUMN ioe.optimization_run.user_id IS
    'Entry 11B6C. HISTORICAL SUBJECT CORRELATOR, not a live foreign key — see analysis.analysis_run.user_id.';
COMMENT ON COLUMN ioe.scenario.user_id IS
    'Entry 11B6C. HISTORICAL SUBJECT CORRELATOR, not a live foreign key. Sealed once the scenario completes, so it cannot be rewritten even deliberately.';
COMMENT ON COLUMN ioe.integrity_check.user_id IS
    'Entry 11B6C. HISTORICAL SUBJECT CORRELATOR, not a live foreign key. Immutable: the append-only guard refuses every UPDATE once the row is terminal.';

-- ---------------------------------------------------------------------------
-- 2. Transitional guard: the application must not gain a delete it never had
-- ---------------------------------------------------------------------------
-- Until now `DELETE FROM identity.user_account` was refused for an ACCIDENTAL
-- reason — the cascade hit append-only evidence. Section 1 removes that
-- accident. Measured before doing so, as the runtime login rather than from the
-- catalogue:
--
--     onyx_app_rw   has DELETE, sees every account, no RLS on the table
--     onyx_app_ro   no DELETE
--     PUBLIC        no DELETE
--     privacy/freshness workers   cannot even reach the table
--
-- So without this, 0060 would hand the ordinary HTTP role the ability to erase
-- any account outright, with no lifecycle, while 63 privacy surfaces are still
-- unclassified. That is not a theoretical exposure; it is one statement.
--
-- The privilege is simply withdrawn. Nothing in `app/` or `workers/` issues
-- such a DELETE (the only reads of UserAccount are auth lookups), so this
-- removes a capability that was never exercised. It is a real boundary rather
-- than a convention, and it does not invent any lifecycle semantics: the future
-- terminal-removal keyhole will be a SECURITY DEFINER function owned by the
-- migrator and granted to the privacy worker, which this does not obstruct.
--
-- Deliberately NOT a BEFORE DELETE trigger. A trigger would also block the
-- owner-level diagnostic deletes that the PD-9 durable-ledger suite depends on,
-- and would duplicate a boundary the privilege system already expresses.

REVOKE DELETE ON identity.user_account FROM onyx_app_rw;

COMMENT ON TABLE identity.user_account IS
    'The account root. Entry 11B6C withdrew DELETE from onyx_app_rw: terminal account removal is a governed lifecycle operation, not something the HTTP role may do, and 0060 removed the accidental cascade blocker that used to prevent it. Owner-level DELETE remains possible and is a migration/diagnostic path, not a product path.';

-- ---------------------------------------------------------------------------
-- 3. The freshness fan-out must not notify a subject that no longer exists
-- ---------------------------------------------------------------------------
-- Found by running the full suite after section 1, not by reading the code.
--
-- `ioe.fan_out_freshness_event` expands a tax-year- or analysis-scoped event
-- into one child event per affected tenant, and it finds those tenants by
-- selecting `user_id` from completed, currently-fresh `ioe.scenario` and
-- `ioe.optimization_run` rows. Those two tables now survive account deletion,
-- so the query can return a subject whose account is gone — and
-- `freshness_outbox.user_id` is still a live foreign key to
-- `identity.user_account`, as it should be, because the outbox is transient
-- state that dies with the account.
--
-- The insert therefore raises ForeignKeyViolation, and because the fan-out is
-- one statement over every affected tenant, ONE deleted account fails the whole
-- broadcast for everybody else in it. That is a cross-tenant availability
-- failure created by section 1, so it is fixed in the same migration.
--
-- The outbox foreign key is deliberately left alone. Filtering the producer is
-- correct on the merits rather than merely convenient: freshness means "your
-- current advice may be stale", a deleted account has nobody to tell, and its
-- retained rows are historical evidence rather than live advice.
--
-- Reproduced from the committed text of 29_ioe_outbox_and_projection.sql with
-- exactly one addition — the `live` CTE — so no other behaviour drifts.

CREATE OR REPLACE FUNCTION ioe.fan_out_freshness_event(
    p_event_id  uuid,
    p_worker_id text
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ioe, pg_catalog
AS $$
DECLARE
    v_row   ioe.freshness_outbox%ROWTYPE;
    v_count integer := 0;
BEGIN
    SELECT * INTO v_row FROM ioe.freshness_outbox WHERE id = p_event_id FOR UPDATE;
    IF NOT FOUND OR v_row.claim_state <> 'claimed'
       OR v_row.claimed_by IS DISTINCT FROM p_worker_id THEN
        RETURN 0;
    END IF;
    IF v_row.user_id IS NOT NULL THEN
        RETURN 0;                          -- already single-tenant
    END IF;

    -- Reads user_id ONLY, to decide who has work pending. No financial column
    -- is selected, and nothing outside the outbox is written.
    WITH affected AS (
        SELECT DISTINCT s.user_id
          FROM ioe.scenario s
         WHERE s.workflow_status = 'completed'
           AND s.freshness_status = 'current'
           AND (v_row.tax_year IS NULL OR s.tax_year = v_row.tax_year)
           AND (v_row.analysis_id IS NULL OR s.base_analysis_id = v_row.analysis_id)
        UNION
        SELECT DISTINCT r.user_id
          FROM ioe.optimization_run r
         WHERE r.workflow_status = 'completed'
           AND r.freshness_status = 'current'
           AND (v_row.tax_year IS NULL OR r.tax_year = v_row.tax_year)
           AND (v_row.analysis_id IS NULL OR r.analysis_id = v_row.analysis_id)
    ), live AS (
        -- Entry 11B6C. Retained evidence outlives its account now, so `affected`
        -- can name a subject with no `identity.user_account` row. Enqueuing for
        -- one violates `freshness_outbox_user_id_fkey` and takes the whole
        -- broadcast batch down with it — other tenants included. Freshness means
        -- "your current advice may be stale"; a deleted account has nobody to
        -- tell, and its retained rows are history rather than live advice.
        SELECT a.user_id
          FROM affected a
         WHERE EXISTS (SELECT 1 FROM identity.user_account u WHERE u.id = a.user_id)
    ), inserted AS (
        INSERT INTO ioe.freshness_outbox
            (event_type, stale_reason_code, user_id, analysis_id, tax_year, dedupe_key)
        SELECT v_row.event_type, v_row.stale_reason_code, a.user_id,
               v_row.analysis_id, v_row.tax_year,
               v_row.dedupe_key || ':' || a.user_id::text
          FROM live a
        ON CONFLICT (dedupe_key) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO v_count FROM inserted;
    RETURN v_count;
END;
$$;
