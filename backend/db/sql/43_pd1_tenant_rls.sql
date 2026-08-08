-- =============================================================================
-- Onyx Ledger — 43 · PD-1 tenant RLS for the 16 child tables
-- Alembic revision: 0049_pd1_tenant_rls
--
-- Entry 11B1. PD-1, as recorded in the Entry 11A gap register:
--
--     16 tenant-owned child tables have no RLS, including the frozen snapshot,
--     extraction fields, AI messages and AI prompt context. (27 user-derived
--     tables lack RLS in total; 11 of those — `identity` and `audit` —
--     correctly cannot have it.)
--
-- THE DEFECT, REPRODUCED BEFORE IT WAS FIXED
-- `tests/security/test_pd1_tenant_isolation.py` becomes `onyx_app_rw`, sets
-- `app.user_id` the way `unit_of_work` does, and queries these tables directly.
-- Against the previous schema tenant A could read, update and delete tenant B's
-- rows in all 16, insert rows into B's tree in all 16, reassign a child into
-- B's tree, and — with no tenant context at all — read everything.
--
-- Entry 11A was right that no API path does this today. That is the point: the
-- protection was every service remembering to scope through the parent, and the
-- repository elsewhere treats RLS as "the tenant-correctness boundary, not the
-- query access path". One careless future query, or one privileged deletion
-- phase walking these tables, crosses tenants silently.
--
-- WHY THIS BLOCKS DELETION SPECIFICALLY
-- The later purge phases resolve what belongs to an account and then act on it.
-- Doing that across a boundary the database does not enforce means a mistake
-- deletes or exposes somebody else's frozen snapshot, extraction fields, AI
-- messages or invoices — and the failure is silent in exactly the direction
-- nobody checks, because a purge that deletes too much looks like it worked.
--
-- THE MODEL
-- None of the 16 has a `user_id` of its own. Every one reaches its owner
-- through a NOT NULL parent foreign key, and the policy resolves that chain to
-- `ref.current_app_user()` EXPLICITLY rather than leaning on the parent's own
-- RLS to filter the subquery. Chaining would work — a parent policy does apply
-- inside a child's EXISTS — but it makes each child's isolation depend on a
-- policy on another table, and that dependency is invisible when reading the
-- child. This matches the idiom Entry 3B already used for the 18 `ioe` children.
--
-- A denormalized `user_id` on each child was considered and rejected: it would
-- add a writable ownership column to sealed evidence (`analysis_input_snapshot`,
-- `run_rule_snapshot`), which creates a way to *change* ownership that does not
-- exist today, and it would need a backfill plus a trigger to keep it true.
-- The parent pointer is already NOT NULL and already immutable in practice.
--
-- `FOR ALL` WITH BOTH `USING` AND `WITH CHECK`
-- A `USING`-only policy protects reads and leaves ownership forgery open: a
-- caller can still INSERT a row into somebody else's tree, or UPDATE a child's
-- parent pointer to move it there. Both were reproduced. Every policy below
-- therefore carries `WITH CHECK` with the same predicate, which is also what
-- the existing 48 tenant policies in this database do.
--
-- DENY BY DEFAULT FALLS OUT OF THE PREDICATE
-- `ref.current_app_user()` returns NULL when `app.user_id` is unset — a login
-- or a system worker — and `NULL = anything` is never true, so an anonymous
-- session matches no rows without needing a rule that says so.
--
-- GRANTS ARE A SEPARATE CONTROL AND ARE LEFT AS THEY ARE
-- `onyx_app_rw` keeps SELECT/INSERT/UPDATE/DELETE and `onyx_app_ro` keeps
-- SELECT, matching every one of these tables' parents. RLS decides WHICH rows;
-- the grant decides WHETHER the command is available at all. This entry changes
-- the first and deliberately not the second — a privilege the product uses
-- today should not be removed on the theory that a future privacy worker will
-- want its own boundary. That worker gets its own keyhole when it is written.
--
-- FORCE ROW LEVEL SECURITY on every table, so the owner is subject to the
-- policies too and a migration cannot quietly read across tenants.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1 · Depth-1: the parent carries `user_id` directly
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    -- child fully-qualified, child FK column, parent fully-qualified
    spec text[][] := ARRAY[
        ['ai.ai_message',                    'conversation_id',   'ai.ai_conversation'],
        ['analysis.analysis_assumption',     'analysis_id',       'analysis.analysis_run'],
        ['analysis.analysis_input_snapshot', 'analysis_id',       'analysis.analysis_run'],
        ['analysis.analysis_line_item',      'analysis_id',       'analysis.analysis_run'],
        ['analysis.reconciliation_check',    'analysis_id',       'analysis.analysis_run'],
        ['billing.invoice',                  'subscription_id',   'billing.subscription'],
        ['docs.document_extraction',         'document_id',       'docs.document'],
        ['docs.document_link',               'document_id',       'docs.document'],
        -- The owner is the RECOMMENDATION's user. `actor_user_id` on this table
        -- is whoever changed the status, which may be an operator — using it
        -- would invent tenant ownership from a loosely related column.
        ['reco.recommendation_status_event', 'recommendation_id', 'reco.recommendation'],
        ['wealth.asset_valuation',           'asset_id',          'wealth.asset'],
        ['wealth.liability_balance',         'liability_id',      'wealth.liability'],
        ['wealth.registered_account_detail', 'asset_id',          'wealth.asset']
    ];
    child text; fk text; parent text;
    sch text; tbl text; policy text; predicate text;
BEGIN
    FOR i IN 1 .. array_length(spec, 1) LOOP
        child  := spec[i][1];
        fk     := spec[i][2];
        parent := spec[i][3];
        sch := split_part(child, '.', 1);
        tbl := split_part(child, '.', 2);
        policy := 'p_self_' || tbl;

        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY;', child);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY;', child);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %s;', policy, child);

        predicate := format(
            'EXISTS (SELECT 1 FROM %s p WHERE p.id = %I.%I '
            '          AND p.user_id = ref.current_app_user())',
            parent, tbl, fk);

        EXECUTE format(
            'CREATE POLICY %I ON %s FOR ALL USING (%s) WITH CHECK (%s);',
            policy, child, predicate, predicate);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 2 · Depth-2: the grandparent carries `user_id`
--
-- Resolved in one predicate rather than by trusting the intermediate table's
-- own policy. `docs.extraction_field` and the two `ai_message` children are all
-- one hop further out than section 1.
-- ---------------------------------------------------------------------------
ALTER TABLE ai.ai_message_citation ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai.ai_message_citation FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_self_ai_message_citation ON ai.ai_message_citation;
CREATE POLICY p_self_ai_message_citation ON ai.ai_message_citation
    FOR ALL
    USING (EXISTS (
        SELECT 1 FROM ai.ai_message m
          JOIN ai.ai_conversation c ON c.id = m.conversation_id
         WHERE m.id = ai_message_citation.message_id
           AND c.user_id = ref.current_app_user()))
    WITH CHECK (EXISTS (
        SELECT 1 FROM ai.ai_message m
          JOIN ai.ai_conversation c ON c.id = m.conversation_id
         WHERE m.id = ai_message_citation.message_id
           AND c.user_id = ref.current_app_user()));

ALTER TABLE ai.ai_prompt_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai.ai_prompt_context FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_self_ai_prompt_context ON ai.ai_prompt_context;
CREATE POLICY p_self_ai_prompt_context ON ai.ai_prompt_context
    FOR ALL
    USING (EXISTS (
        SELECT 1 FROM ai.ai_message m
          JOIN ai.ai_conversation c ON c.id = m.conversation_id
         WHERE m.id = ai_prompt_context.message_id
           AND c.user_id = ref.current_app_user()))
    WITH CHECK (EXISTS (
        SELECT 1 FROM ai.ai_message m
          JOIN ai.ai_conversation c ON c.id = m.conversation_id
         WHERE m.id = ai_prompt_context.message_id
           AND c.user_id = ref.current_app_user()));

ALTER TABLE docs.extraction_field ENABLE ROW LEVEL SECURITY;
ALTER TABLE docs.extraction_field FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_self_extraction_field ON docs.extraction_field;
CREATE POLICY p_self_extraction_field ON docs.extraction_field
    FOR ALL
    USING (EXISTS (
        SELECT 1 FROM docs.document_extraction e
          JOIN docs.document d ON d.id = e.document_id
         WHERE e.id = extraction_field.extraction_id
           AND d.user_id = ref.current_app_user()))
    WITH CHECK (EXISTS (
        SELECT 1 FROM docs.document_extraction e
          JOIN docs.document d ON d.id = e.document_id
         WHERE e.id = extraction_field.extraction_id
           AND d.user_id = ref.current_app_user()));

-- ---------------------------------------------------------------------------
-- 3 · Two-branch ownership
--
-- `ioe.run_rule_snapshot` hangs off EITHER an optimization run OR a scenario —
-- `CHECK (num_nonnulls(run_id, scenario_id) = 1)` enforces exactly one. A
-- single-branch policy would deny every row of the other kind, which is an
-- outage rather than isolation, so both branches are named. Both parents
-- already carry `user_id` and their own RLS.
-- ---------------------------------------------------------------------------
ALTER TABLE ioe.run_rule_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE ioe.run_rule_snapshot FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_self_run_rule_snapshot ON ioe.run_rule_snapshot;
CREATE POLICY p_self_run_rule_snapshot ON ioe.run_rule_snapshot
    FOR ALL
    USING (
        EXISTS (SELECT 1 FROM ioe.optimization_run r
                 WHERE r.id = run_rule_snapshot.run_id
                   AND r.user_id = ref.current_app_user())
        OR EXISTS (SELECT 1 FROM ioe.scenario s
                    WHERE s.id = run_rule_snapshot.scenario_id
                      AND s.user_id = ref.current_app_user()))
    WITH CHECK (
        EXISTS (SELECT 1 FROM ioe.optimization_run r
                 WHERE r.id = run_rule_snapshot.run_id
                   AND r.user_id = ref.current_app_user())
        OR EXISTS (SELECT 1 FROM ioe.scenario s
                    WHERE s.id = run_rule_snapshot.scenario_id
                      AND s.user_id = ref.current_app_user()));

-- ---------------------------------------------------------------------------
-- 4 · Indexes supporting the policy predicates
--
-- Every predicate is `parent.id = child.fk`, so the parent side is a primary
-- key lookup and needs nothing. The CHILD side is what a sequential scan would
-- punish: filtering a child table by its parent pointer. Added only where the
-- column is not already the leading column of an existing index — see
-- §31 of the entry for the measurements that justify each one.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_ai_message_citation_message
    ON ai.ai_message_citation (message_id);
CREATE INDEX IF NOT EXISTS ix_ai_prompt_context_message
    ON ai.ai_prompt_context (message_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_check_analysis
    ON analysis.reconciliation_check (analysis_id);
CREATE INDEX IF NOT EXISTS ix_analysis_assumption_analysis
    ON analysis.analysis_assumption (analysis_id);
CREATE INDEX IF NOT EXISTS ix_invoice_subscription
    ON billing.invoice (subscription_id);
CREATE INDEX IF NOT EXISTS ix_recommendation_status_event_recommendation
    ON reco.recommendation_status_event (recommendation_id);
CREATE INDEX IF NOT EXISTS ix_run_rule_snapshot_run
    ON ioe.run_rule_snapshot (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_run_rule_snapshot_scenario
    ON ioe.run_rule_snapshot (scenario_id) WHERE scenario_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 5 · Comments, so the next reader does not have to re-derive the chain
-- ---------------------------------------------------------------------------
COMMENT ON POLICY p_self_ai_message_citation ON ai.ai_message_citation IS
  'Tenant isolation via ai_message → ai_conversation.user_id. PD-1, Entry 11B1.';
COMMENT ON POLICY p_self_ai_prompt_context ON ai.ai_prompt_context IS
  'Tenant isolation via ai_message → ai_conversation.user_id. PD-1, Entry 11B1.';
COMMENT ON POLICY p_self_extraction_field ON docs.extraction_field IS
  'Tenant isolation via document_extraction → document.user_id. PD-1, Entry 11B1.';
COMMENT ON POLICY p_self_run_rule_snapshot ON ioe.run_rule_snapshot IS
  'Tenant isolation via EITHER optimization_run OR scenario; the table CHECKs that exactly one is set. PD-1, Entry 11B1.';
