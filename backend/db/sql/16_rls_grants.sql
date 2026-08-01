-- =============================================================================
-- Onyx Ledger — 16 · Row-Level Security + role grants
-- Alembic revision: 0017_rls_grants
-- Defense in depth: every user-owned table is filtered to the authenticated
-- user via the app.user_id GUC. Least-privilege object grants per role.
-- =============================================================================

-- ---- Object grants ----------------------------------------------------------
-- Runtime API role: CRUD on user domains + read on KB/ref.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA
    identity, profile, finance, wealth, analysis, reco, ai, docs, billing
    TO onyx_app_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA ref, tax_kb, rules TO onyx_app_rw;
GRANT INSERT ON audit.consent_log, audit.data_export_request,
    audit.data_deletion_request, audit.security_event TO onyx_app_rw;

-- Read-only reporting role.
GRANT SELECT ON ALL TABLES IN SCHEMA
    ref, identity, profile, finance, wealth, tax_kb, rules,
    analysis, reco, ai, docs, billing TO onyx_app_ro;

-- KB authoring role: writes only tax_kb + rules.
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA tax_kb, rules TO onyx_kb_admin;
GRANT SELECT ON ALL TABLES IN SCHEMA ref TO onyx_kb_admin;

-- Audit writer: INSERT-only into the log (the trigger runs as table owner, but
-- this covers any direct writer path). No SELECT on PII.
GRANT INSERT ON audit.audit_log TO onyx_audit_writer;

-- Sequences (identity columns are UUID defaults, but grant for safety on any serial).
GRANT USAGE ON ALL SEQUENCES IN SCHEMA
    identity, profile, finance, wealth, analysis, reco, ai, docs, billing
    TO onyx_app_rw;

-- ---- Row-Level Security -----------------------------------------------------
-- Helper: the current app user (NULL if unset → policies deny).
CREATE OR REPLACE FUNCTION ref.current_app_user()
RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.user_id', true), '')::uuid;
$$;

-- Enable RLS + a self-ownership policy on every user-owned DATA table.
-- NOTE: the identity/auth tables are deliberately excluded — registration and
-- login operate with NO user context yet, so they cannot satisfy a self-
-- ownership policy. Those tables are gated exclusively by the Auth service
-- (which is the only code path that touches them). RLS defends the tables that
-- actually hold user PII and financial data.
DO $$
DECLARE
    owned text[] := ARRAY[
        'profile.user_profile','profile.tax_profile','profile.dependent',
        'profile.spouse_profile','profile.user_preference','profile.user_privacy_setting',
        'finance.income_source','finance.expense_record',
        'wealth.asset','wealth.liability',
        'analysis.analysis_run',
        'reco.recommendation','reco.recommendation_feedback',
        'ai.ai_conversation',
        'docs.document',
        'billing.subscription','billing.payment_method_ref','billing.entitlement'
    ];
    fq text; sch text; tbl text;
BEGIN
    FOREACH fq IN ARRAY owned LOOP
        sch := split_part(fq, '.', 1);
        tbl := split_part(fq, '.', 2);
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY;', sch, tbl);
        EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY;', sch, tbl);
        EXECUTE format(
            'CREATE POLICY p_self_%s ON %I.%I
             USING (user_id = ref.current_app_user())
             WITH CHECK (user_id = ref.current_app_user());',
            tbl, sch, tbl);
    END LOOP;
END $$;

-- Default privileges so future tables created by the migrator inherit grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA
    identity, profile, finance, wealth, analysis, reco, ai, docs, billing
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO onyx_app_rw;
