-- Entry 11B6B — the audit writer records a SUBJECT (PD-15)
--
-- Entry 11B6 added `audit.audit_log.subject_key` and 11B6A added
-- `identity.subject_key_for`, and nothing populated the column. A
-- de-identification with nothing to de-identify is not a fix. This is the
-- load-bearing half: the subject is written when the immutable row is
-- inserted, because an append-only table cannot be corrected later.
--
-- WHY A NEW FILE INSTEAD OF EDITING 42_audit_payload_minimization.sql.
-- `apply_sql_file` reads its file AT MIGRATION RUN TIME, and revision 0048
-- applies 42. Editing that file would change what 0048 installs on a fresh
-- database — the revision would stop representing what it represented when it
-- was written. The repository has held that line without exception: 42, 45, 48
-- and 49 each have exactly one commit, the one that introduced them alongside
-- their migration. So every change gets a new file and a new revision, and
-- this is that file.
--
-- HOW THIS FUNCTION WAS PRODUCED. Not from memory. The previous attempt wrote
-- it out by hand and invented `audit.payload_policy`, where the real code
-- calls `audit.audit_retains_values()` and `audit.audit_structural_columns()`.
-- The body below was produced by reading the certified definition out of
-- 42_audit_payload_minimization.sql and applying four textual edits:
--
--   1. declare v_subject_user / v_subject_key
--   2. resolve the subject from the RAW record, before minimisation
--   3. add subject_key to the INSERT
--   4. widen search_path to reach identity.subject_key_for
--
-- Everything else — the actor resolution, the payload minimisation, the
-- credential-column list, the structural-column policy — is byte-identical to
-- the certified original, and a test compares them.
--
-- ACTOR IS NOT SUBJECT. An operator disabling a customer's account records the
-- operator in actor_id and the customer in subject_key. De-identifying the
-- customer severs the subject and leaves the operator accountable. Keying on
-- actor_id would have done exactly the wrong thing in both directions, which
-- is PD-15.

CREATE OR REPLACE FUNCTION audit.log_change()
RETURNS trigger
RETURNS NULL ON NULL INPUT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = audit, identity, pg_catalog
AS $$
DECLARE
    v_actor uuid;
    v_actor_type text;
    v_entity_id text;
    v_subject_user uuid;
    v_subject_key uuid;
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

    -- THE SUBJECT — the account this change CONCERNED, which is not the actor.
    -- Read from the raw record and BEFORE the payload minimisation below,
    -- which replaces values with '[redacted]': a redacted user_id would
    -- silently yield no subject, and that failure would look exactly like an
    -- event that legitimately has none.
    --
    -- Resolved from a typed column, never inferred from actor_id, from
    -- entity_id's format, or by searching the payload for something that looks
    -- like an identifier. That inference is PD-15.
    BEGIN
        v_subject_user := nullif(
            CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ->> 'user_id'
                 ELSE to_jsonb(NEW) ->> 'user_id' END, '')::uuid;
        IF v_subject_user IS NULL
           AND TG_TABLE_SCHEMA = 'identity' AND TG_TABLE_NAME = 'user_account'
        THEN
            -- The row IS the account.
            v_subject_user := nullif(
                CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ->> 'id'
                     ELSE to_jsonb(NEW) ->> 'id' END, '')::uuid;
        END IF;
    EXCEPTION WHEN others THEN
        -- A non-uuid user_id on some future table is a reason to record no
        -- subject, not a reason to lose the audit row.
        v_subject_user := NULL;
    END;
    v_subject_key := identity.subject_key_for(v_subject_user);

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
        previous_value, new_value, subject_key)
    VALUES (
        v_actor_type, v_actor, TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME, v_entity_id,
        v_old, v_new, v_subject_key);

    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

COMMENT ON FUNCTION audit.log_change() IS
  'Append-only change audit. Records the ACTOR (who acted) and the SUBJECT (which account the change concerned) as separate things — Entry 11B6B, PD-15 — with the subject resolved from a typed column on the changed row and never inferred from actor_id or from payload text. For personal-data relations the payload keeps its column names and loses its values.';

REVOKE EXECUTE ON FUNCTION audit.log_change() FROM PUBLIC;
