-- =============================================================================
-- Onyx Ledger — 40 · Audit secret redaction  (schema: audit)
-- Alembic revision: 0046_audit_secret_redaction
--
-- Entry 11A, narrow fix for an ACTIVE privacy/security defect found during the
-- data-lifecycle audit.
--
-- THE DEFECT
-- `audit.log_change` copies the WHOLE row with `to_jsonb(OLD)` / `to_jsonb(NEW)`
-- for all 32 audited tables. One of those tables is `identity.user_credential`,
-- so every registration and every password change wrote the account's Argon2
-- hash into `audit.audit_log`. Proven by probe, not inferred: one registration
-- produced an INSERT row whose `new_value` contained `password_hash`.
--
-- Why that is worse than it first looks:
--
--   * `audit.audit_log` carries a `reject_mutation` trigger, so the copy is
--     APPEND-ONLY — it cannot be updated or deleted through any ordinary path;
--   * it has NO foreign key to `identity.user_account`, so no cascade reaches
--     it and account deletion cannot remove it;
--   * it is partitioned by month and read under different grants from the
--     credential table it duplicates.
--
-- So a credential-equivalent secret was being copied out of the one table
-- designed to hold it into a store that is deliberately impossible to erase.
--
-- THE FIX, AND ITS DELIBERATE NARROWNESS
-- The audit record still says that the row changed, who changed it, and which
-- columns it has — only the VALUE of a named secret column is replaced with a
-- marker. Nothing else about auditing changes: the same tables are audited, the
-- same operations, the same shape of record. An auditor can still see that a
-- credential was created or rotated, which is the reason to audit it; the hash
-- itself was never evidence of anything.
--
-- The redaction list is explicit rather than pattern-matched. A regex over
-- column names would quietly redact `optimization_result_hash` and
-- `manifest_hash`, which are content addresses of sealed evidence and MUST stay
-- in the audit trail — removing them would damage replay verification to fix a
-- credential problem.
--
-- WHAT THIS DOES NOT DO
-- It does not address the broader finding that `audit.audit_log` holds whole
-- copies of financial rows with no path to deletion. That is a design decision
-- about what an audit record should contain, it affects the Entry 3A audit
-- guarantees, and it belongs in Entry 11B with its own tests. It is recorded as
-- PD-4 in docs/privacy/data-lifecycle-specification.md.
--
-- FUNCTION-ONLY. No table, column, index, constraint, policy, grant or row is
-- touched, and no existing audit row is modified — the log is append-only and
-- rows already written keep whatever they already contain.
-- =============================================================================

CREATE OR REPLACE FUNCTION audit.log_change()
RETURNS trigger
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
    -- Column names whose VALUE must never be copied into the audit log.
    -- Explicit, not a pattern: `%hash%` would also match the sealed-evidence
    -- content addresses, which the audit trail exists to preserve.
    v_secret_columns text[] := ARRAY[
        'password_hash',
        'refresh_token_hash',
        'token_hash',
        'reset_token_hash',
        'verification_token_hash',
        'mfa_secret',
        'secret'
    ];
    v_column text;
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

    -- The key is KEPT and its value replaced, so the record still shows that
    -- the column exists and was written. Dropping the key would make a rotated
    -- credential indistinguishable from one that was never set.
    FOREACH v_column IN ARRAY v_secret_columns LOOP
        IF v_old ? v_column THEN
            v_old := jsonb_set(v_old, ARRAY[v_column], '"[redacted]"'::jsonb);
        END IF;
        IF v_new ? v_column THEN
            v_new := jsonb_set(v_new, ARRAY[v_column], '"[redacted]"'::jsonb);
        END IF;
    END LOOP;

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
  'Append-only change audit. Copies the changed row, with the value of named credential columns replaced by a redaction marker: the audit log is immutable and unreachable by account deletion, so a secret written into it can never be removed.';
