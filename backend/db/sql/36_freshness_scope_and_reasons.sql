-- =============================================================================
-- Onyx Ledger — 36 · Freshness scope and reasons  (schema: ioe)
-- Alembic revision: 0042_freshness_scope_and_reasons
--
-- Entry 9, remaining domains. Two real gaps, both needing schema.
--
-- 1. ANALYSIS SUPERSESSION had no distinct reason. A newly completed analysis
--    reported `BASELINE_INPUTS_CHANGED`, which is the reason for "your income
--    changed" — a different thing from "a newer analysis exists". A user acting
--    on the first would go and check their figures; on the second they would
--    open the new analysis. `NEWER_ANALYSIS_AVAILABLE` separates them.
--
-- 2. RULE FAN-OUT WAS TOO BROAD. A rule published for Ontario staled every
--    completed result for that tax year in every jurisdiction, because the
--    outbox could express a year but not a jurisdiction. Over-staling is not
--    harmless: it trains people to ignore the label. `jurisdiction` is a
--    QUALIFIER, not a rival scope — it narrows a tax-year event rather than
--    replacing it, which is why `freshness_outbox_scope_is_singular` (over
--    analysis_id and tax_year) is left exactly as it is.
--
-- Three new registry event types are admitted at the same time so registry
-- activation can name what actually moved.
--
-- ADDITIVE: one nullable column, one index, two widened CHECK constraints.
-- Every existing row satisfies the new constraints, so the rewrites are
-- validation passes with no data change. Historical rows keep NULL
-- jurisdiction, which means "not jurisdiction-scoped" and fans out exactly as
-- it did before.
-- =============================================================================

ALTER TABLE ioe.freshness_outbox
    ADD COLUMN IF NOT EXISTS jurisdiction text;

COMMENT ON COLUMN ioe.freshness_outbox.jurisdiction IS
    'Optional jurisdiction QUALIFIER. NULL means the event is not jurisdiction-scoped and fans out across jurisdictions, which is the historical behaviour. A value narrows fan-out to matching results only.';

-- Finding pending work for one jurisdiction without scanning the queue.
CREATE INDEX IF NOT EXISTS ix_ioe_freshness_outbox_jurisdiction
    ON ioe.freshness_outbox (jurisdiction, tax_year)
    WHERE jurisdiction IS NOT NULL;

-- ---- widened event vocabulary -----------------------------------------------
ALTER TABLE ioe.freshness_outbox
    DROP CONSTRAINT IF EXISTS freshness_outbox_event_type_check;
ALTER TABLE ioe.freshness_outbox
    ADD CONSTRAINT freshness_outbox_event_type_check CHECK (event_type IN (
        'analysis_completed',
        'analysis_superseded',
        'baseline_inputs_changed',
        'financial_data_changed',
        'profile_changed',
        'document_status_changed',
        'rule_published',
        'rule_withdrawn',
        'rule_superseded',
        'reference_data_changed',
        'engine_version_changed',
        'objective_policy_changed',
        'lever_registry_changed',
        'assumption_registry_changed',
        'relationship_registry_changed',
        'support_score_policy_changed',
        'projection_methodology_changed'));

-- ---- widened stale-reason vocabulary ----------------------------------------
ALTER TABLE ioe.scenario
    DROP CONSTRAINT IF EXISTS scenario_stale_reason_check;
ALTER TABLE ioe.scenario
    ADD CONSTRAINT scenario_stale_reason_check CHECK (
        stale_reason_code IS NULL OR stale_reason_code IN (
            'BASELINE_INPUTS_CHANGED',
            'BASELINE_RESULT_CHANGED',
            'NEWER_ANALYSIS_AVAILABLE',
            'RULE_SNAPSHOT_SUPERSEDED',
            'ENGINE_VERSION_CHANGED',
            'REFERENCE_DATA_CHANGED',
            'LEVER_REGISTRY_CHANGED',
            'OBJECTIVE_POLICY_CHANGED',
            'ASSUMPTION_SET_CHANGED',
            'RELATIONSHIP_REGISTRY_CHANGED',
            'SUPPORT_SCORE_POLICY_CHANGED',
            'PROJECTION_METHODOLOGY_CHANGED',
            'TAX_YEAR_ROLLED_OVER',
            'SUPERSEDED_BY_REFRESH'));

-- ---- claim keyhole: carry the jurisdiction qualifier ------------------------
-- The keyhole stays identifier-only. A jurisdiction code is a bounded province
-- code from `ref.province`, not a financial value, so returning it does not
-- widen what a compromised worker could learn: it already receives the tax year
-- and the tenant id.
-- The OUT row type changes, and PostgreSQL will not replace a function whose
-- signature it cannot keep. Dropped and recreated in the same transaction as
-- its grants, so no window exists in which the worker holds no EXECUTE.
DROP FUNCTION IF EXISTS ioe.claim_freshness_events(integer, text);

CREATE FUNCTION ioe.claim_freshness_events(
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
                  o.analysis_id, o.tax_year, o.jurisdiction, o.user_id
    ), logged AS (
        INSERT INTO ioe.freshness_outbox_audit
            (event_id, transition, worker_id, claim_token)
        SELECT id, 'claimed', p_worker_id, claim_token FROM claimed
        RETURNING 1
    )
    SELECT c.id, c.claim_token, c.stale_reason_code,
           c.analysis_id, c.tax_year, c.jurisdiction, c.user_id
      FROM claimed c
     ORDER BY c.id;
END;
$$;

ALTER FUNCTION ioe.claim_freshness_events(integer, text) OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION ioe.claim_freshness_events(integer, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ioe.claim_freshness_events(integer, text)
    TO onyx_freshness_worker;
