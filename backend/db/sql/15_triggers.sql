-- =============================================================================
-- Onyx Ledger — 15 · Triggers (updated_at + immutable audit)
-- Alembic revision: 0016_triggers
-- Attaches the shared updated_at trigger to every table with that column, and
-- a generic audit trigger to the tables we track. Runs AFTER all tables exist.
-- =============================================================================

-- ---- updated_at: auto-attach to every base table that has the column --------
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.table_schema, c.table_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.column_name = 'updated_at'
          AND t.table_type = 'BASE TABLE'
          AND c.table_schema IN ('ref','identity','profile','finance','wealth','tax_kb',
                                 'rules','analysis','reco','ai','docs','admin','billing')
    LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON %I.%I
             FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();',
            r.table_schema, r.table_name);
    END LOOP;
END $$;

-- ---- Generic audit trigger --------------------------------------------------
-- Writes a row into audit.audit_log for every INSERT/UPDATE/DELETE on tracked
-- tables. Actor is read from the session GUC app.user_id (set by the API).
CREATE OR REPLACE FUNCTION audit.log_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_actor uuid;
    v_actor_type text;
    v_entity_id text;
BEGIN
    BEGIN
        v_actor := current_setting('app.user_id', true)::uuid;
    EXCEPTION WHEN others THEN
        v_actor := NULL;
    END;
    v_actor_type := coalesce(current_setting('app.actor_type', true), 'system');

    v_entity_id := CASE
        WHEN TG_OP = 'DELETE' THEN (to_jsonb(OLD) ->> 'id')
        ELSE (to_jsonb(NEW) ->> 'id')
    END;

    INSERT INTO audit.audit_log (
        actor_type, actor_id, action, entity_schema, entity_table, entity_id,
        previous_value, new_value)
    VALUES (
        v_actor_type, v_actor, TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME, v_entity_id,
        CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN to_jsonb(OLD) END,
        CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN to_jsonb(NEW) END);

    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

-- Attach the audit trigger to the tables that carry legal/financial weight.
DO $$
DECLARE
    tracked text[] := ARRAY[
        'identity.user_account','identity.user_credential',
        'profile.tax_profile','profile.dependent','profile.spouse_profile',
        'finance.income_source','finance.expense_record',
        'wealth.asset','wealth.liability',
        'tax_kb.tax_rule','tax_kb.tax_rule_version',
        'rules.rule_condition','rules.rule_outcome','rules.calc_formula',
        'analysis.analysis_run',
        'reco.recommendation',
        'admin.rule_change_request','admin.rule_publication','admin.admin_user',
        'billing.subscription','billing.invoice'
    ];
    fq text; sch text; tbl text;
BEGIN
    FOREACH fq IN ARRAY tracked LOOP
        sch := split_part(fq, '.', 1);
        tbl := split_part(fq, '.', 2);
        EXECUTE format(
            'CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON %I.%I
             FOR EACH ROW EXECUTE FUNCTION audit.log_change();', sch, tbl);
    END LOOP;
END $$;

-- ---- Enforce append-only on the audit log -----------------------------------
CREATE OR REPLACE FUNCTION audit.reject_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.audit_log is append-only';
END; $$;
CREATE TRIGGER trg_audit_immutable
    BEFORE UPDATE OR DELETE ON audit.audit_log
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
