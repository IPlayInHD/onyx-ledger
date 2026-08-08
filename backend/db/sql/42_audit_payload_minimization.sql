-- =============================================================================
-- Onyx Ledger — 42 · Audit payload minimization  (schema: audit)
-- Alembic revision: 0048_audit_payload_minimization
--
-- Entry 11B0. PD-4, as recorded in the Entry 11A gap register:
--
--     `audit.audit_log` holds whole copies of financial/profile rows, has no
--     user FK, is append-only, and account deletion *adds* to it.
--
-- THE DEFECT
-- `audit.log_change` copies the entire row with `to_jsonb(NEW)` for all 33
-- audited relations. Proven through the production API, not inferred: recording
-- one expense wrote the amount, the description and the free-text note into
-- `audit.audit_log` verbatim, and registering one account wrote the email
-- address there.
--
-- Each of the three properties below is defensible alone. Together they are not:
--
--   * the payload is a WHOLE COPY of the source row, so the audit log is a
--     second, complete store of everyone's financial and profile data;
--   * the table has NO foreign key to `identity.user_account`, so no cascade
--     reaches it — the account-deletion orchestration closed in Entry 11B2
--     cannot touch it, and the Entry 11A inventory derives user-derived tables
--     by foreign-key reachability, so it could not even SEE this one;
--   * `trg_audit_immutable` makes it append-only, so nothing removes a row
--     through any ordinary path.
--
-- The result is a growing, undeletable, unowned copy of exactly the data a
-- deletion request is supposed to erase. Deleting user data while this is true
-- would produce an account that is "deleted" and whose salary, employer and
-- email address are still queryable — which is a worse outcome than not having
-- offered deletion, because it would be reported as done.
--
-- WHAT AN AUDIT RECORD ACTUALLY NEEDS
-- Entry 11A stated the criterion: audit still proves who / what / when, no
-- financial value persists, and the Entry 3A governance guarantees are
-- unaffected. That is satisfiable without the values:
--
--   who    → actor_type, actor_id           (columns of the audit row itself)
--   what   → entity_schema, entity_table, entity_id, action, and the KEY SET
--   when   → created_at
--   which  → for an UPDATE, which columns changed  (see the marker below)
--
-- So the keys stay, the change indication stays, and the values go.
--
-- TWO ALLOWLISTS, BOTH DENY-BY-DEFAULT
--
-- 1. `audit.audit_retains_values` — the OPERATOR plane. Legislation, rule
--    versions, publications, change requests and import jobs are not personal
--    data, and Entry 3A's four-eyes governance rests on being able to read what
--    a rule changed FROM and TO. Those tables keep their payloads whole.
--    Everything else is filtered, so a table added tomorrow is filtered unless
--    somebody deliberately exempts it.
--
-- 2. `audit.audit_structural_columns` — the columns whose VALUES survive
--    filtering. Identifiers, lifecycle timestamps, row versions, workflow
--    status, and the content addresses of sealed evidence.
--
-- WHY THE SEALED HASHES SURVIVE
-- Entry 11A already refused to let a `%hash%` pattern strip
-- `optimization_result_hash` and `manifest_hash`, because the append-only audit
-- copy is what makes the sealed row TAMPER-EVIDENT: if the live hash were ever
-- altered, the audit copy would disagree. Removing them to fix a privacy defect
-- would trade one guarantee for another. They are named explicitly, and
-- `tests/security/test_audit_payload_privacy.py` fails if a new `%_hash%`
-- column appears on an audited sealed table without being listed — otherwise
-- the deny-by-default rule would silently start eroding replay evidence.
--
-- NOTE ON `admin.admin_user`
-- Filtered, not retained. It is an identity table, not a governance decision:
-- four-eyes needs to know WHICH admin id approved a change, which lives in
-- `admin.rule_change_request`, not what their email address is.
--
-- FUNCTION-ONLY. No table, column, index, constraint, policy or grant is
-- touched, and no existing audit row is modified — the log is append-only and
-- rows already written keep whatever they already contain. Historical rows are
-- the separate, explicit job of `scripts/audit_payload_scan.py`.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1 · The operator plane, whose payloads are not personal data
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.audit_retains_values(
    p_schema text, p_table text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog
AS $$
    SELECT (p_schema || '.' || p_table) = ANY (ARRAY[
        -- Legislation and the rule engine. Published reference data; identical
        -- for every user and containing nobody's figures.
        'tax_kb.tax_rule',
        'tax_kb.tax_rule_version',
        'rules.rule_condition',
        'rules.rule_outcome',
        'rules.calc_formula',
        -- Governance decisions. Entry 3A's four-eyes tests read what changed.
        'admin.rule_change_request',
        'admin.rule_publication',
        -- Legislation ingestion, operator-scoped.
        'tkms.import_job',
        'tkms.rollback_record',
        -- Engine configuration, not a person.
        'ioe.weight_config'
    ]);
$$;

COMMENT ON FUNCTION audit.audit_retains_values(text, text) IS
  'True for audited relations whose payload is operator/reference data rather than personal data. Deny-by-default: an unlisted relation has its payload values filtered.';

-- ---------------------------------------------------------------------------
-- 2 · The columns whose values survive filtering
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.audit_structural_columns()
RETURNS text[]
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog
AS $$
    SELECT ARRAY[
        -- Identifiers. UUIDs are pseudonymous, not anonymous — but the audit
        -- row already carries actor_id and entity_id, so keeping them here
        -- adds no linkage that the row did not already have, and losing them
        -- would make a child row impossible to attribute to its parent.
        'id', 'user_id', 'analysis_id', 'subscription_id', 'optimization_run_id',
        'scenario_id', 'admin_id', 'tax_rule_version_id',

        -- Lifecycle. When a row appeared, changed or was soft-deleted.
        'created_at', 'updated_at', 'deleted_at',

        -- Concurrency and workflow state. Not personal; the whole point of
        -- auditing a status column is to see the status.
        'row_version', 'status', 'workflow_status', 'visibility_status',
        'evidence_status', 'verification_status',

        -- SEALED EVIDENCE — content addresses. The append-only audit copy is
        -- what makes the live sealed row tamper-evident. See the header.
        'manifest_hash', 'version_manifest',
        'optimization_result_hash', 'optimization_spec_hash',
        'scenario_result_hash', 'scenario_spec_hash',
        'baseline_input_snapshot_hash', 'baseline_result_hash',

        -- Pinned versions that identify WHICH code and rules produced a sealed
        -- result. Reference identifiers, not user content.
        'engine_version', 'lever_registry_version', 'objective_version',
        'result_schema_version', 'input_execution_policy_version',
        'schema_version', 'version'
    ];
$$;

COMMENT ON FUNCTION audit.audit_structural_columns() IS
  'Columns whose values may be copied into audit.audit_log for a personal-data relation: identifiers, lifecycle timestamps, workflow state, and sealed-evidence content addresses. Every other value is replaced by a marker.';

-- ---------------------------------------------------------------------------
-- 3 · The audit trigger
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.log_change()
RETURNS trigger
RETURNS NULL ON NULL INPUT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = audit, pg_catalog
AS $$
DECLARE
    v_actor uuid;
    v_actor_type text;
    v_entity_id text;
    v_old jsonb;
    v_new jsonb;
    v_retain boolean;
    v_structural text[];
    v_key text;
    -- Named credential columns, from Entry 11A (PD-4a). Kept as a distinct
    -- list because it must apply even to a value-retaining relation:
    -- `admin.admin_user` is filtered anyway, but a future operator table with
    -- a secret must not depend on that.
    v_secret_columns text[] := ARRAY[
        'password_hash',
        'refresh_token_hash',
        'token_hash',
        'reset_token_hash',
        'verification_token_hash',
        'mfa_secret',
        'secret'
    ];
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

    v_old := CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN to_jsonb(OLD) END;
    v_new := CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN to_jsonb(NEW) END;

    -- TG_TABLE_NAME is the PARTITION for a partitioned relation
    -- (`income_source_y2025`, not `income_source`). Deny-by-default is what
    -- makes that safe: a partition is not on the retain list, so it is
    -- filtered, and a new year partition needs no change here.
    v_retain := audit.audit_retains_values(TG_TABLE_SCHEMA, TG_TABLE_NAME);

    IF v_retain THEN
        -- Operator plane. Values kept; named secrets still redacted.
        FOREACH v_key IN ARRAY v_secret_columns LOOP
            IF v_old ? v_key THEN
                v_old := jsonb_set(v_old, ARRAY[v_key], '"[redacted]"'::jsonb);
            END IF;
            IF v_new ? v_key THEN
                v_new := jsonb_set(v_new, ARRAY[v_key], '"[redacted]"'::jsonb);
            END IF;
        END LOOP;
    ELSE
        -- Personal data. The KEY is kept and the VALUE replaced, so the record
        -- still shows which columns the row had and — for an UPDATE — which of
        -- them moved. Dropping the key instead would make a written column
        -- indistinguishable from one that was never set.
        v_structural := audit.audit_structural_columns();

        IF v_old IS NOT NULL THEN
            FOR v_key IN SELECT jsonb_object_keys(v_old) LOOP
                IF NOT (v_key = ANY (v_structural)) THEN
                    v_old := jsonb_set(v_old, ARRAY[v_key],
                                       '"[redacted]"'::jsonb);
                END IF;
            END LOOP;
        END IF;

        IF v_new IS NOT NULL THEN
            FOR v_key IN SELECT jsonb_object_keys(v_new) LOOP
                IF NOT (v_key = ANY (v_structural)) THEN
                    -- `[changed]` carries the one piece of information the
                    -- value was doing for an auditor: that this column moved.
                    -- Comparing the ORIGINAL rows, before either side was
                    -- overwritten, which is why to_jsonb(OLD) is read again
                    -- rather than v_old.
                    IF TG_OP = 'UPDATE'
                       AND (to_jsonb(OLD) -> v_key)
                           IS DISTINCT FROM (to_jsonb(NEW) -> v_key) THEN
                        v_new := jsonb_set(v_new, ARRAY[v_key],
                                           '"[changed]"'::jsonb);
                    ELSE
                        v_new := jsonb_set(v_new, ARRAY[v_key],
                                           '"[redacted]"'::jsonb);
                    END IF;
                END IF;
            END LOOP;
        END IF;
    END IF;

    INSERT INTO audit.audit_log (
        actor_type, actor_id, action, entity_schema, entity_table, entity_id,
        previous_value, new_value)
    VALUES (
        v_actor_type, v_actor, TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME, v_entity_id,
        v_old, v_new);

    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

COMMENT ON FUNCTION audit.log_change() IS
  'Append-only change audit. For personal-data relations the payload keeps its column names and loses its values, with [changed] marking a column an UPDATE moved: audit.audit_log has no user foreign key and is immutable, so a value written into it can never be erased. Operator/reference relations keep their payloads so Entry 3A governance still reads what a rule changed.';

-- Ownership and least privilege are unchanged from 15_triggers.sql: the
-- function is SECURITY DEFINER so that onyx_app_rw, which has NO insert on
-- audit.audit_log, can still be audited. Restated here because a
-- CREATE OR REPLACE does not re-run the original grants and a reader of this
-- file should not have to assume.
REVOKE EXECUTE ON FUNCTION audit.log_change() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION audit.audit_retains_values(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION audit.audit_structural_columns() FROM PUBLIC;
