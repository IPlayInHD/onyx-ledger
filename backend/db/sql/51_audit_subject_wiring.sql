-- Entry 11B6A — production audit writes record a SUBJECT (PD-15)
--
-- Entry 11B6 added `audit.audit_log.subject_key` and the mappings it resolves
-- through, and nothing populated it. A de-identification that has nothing to
-- de-identify is not a fix, so this is the load-bearing half.
--
-- WHERE THE SUBJECT COMES FROM. `audit.log_change` already reads the changed
-- row; the account a change CONCERNS is that row's `user_id`, or its own `id`
-- when the row is the account. It is read from the RAW record, before the
-- payload minimisation runs, because that pass replaces values with
-- '[redacted]' and a redacted user_id would silently produce no subject at all
-- — a failure that would look exactly like an event that legitimately has no
-- subject.
--
-- WHY NOT actor_id. That is PD-15 in one line: an operator disabling a
-- customer's account is the ACTOR, and the customer is the SUBJECT. Recording
-- only the actor means a de-identification either misses the customer or
-- rewrites the operator's accountability. Both columns now exist and mean
-- different things.
--
-- NO PAYLOAD PARSING. The subject is resolved from a typed column on the
-- changed row, never by searching free-form JSON for something that looks like
-- an identifier.

-- The mapping is minted on demand: the first audit event that concerns an
-- account creates its subject key. SECURITY DEFINER because
-- `identity.account_subject` grants nothing to any application role, and the
-- audit trigger runs as whatever role performed the write.
CREATE OR REPLACE FUNCTION identity.subject_key_for(p_user_id uuid)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
DECLARE
    v_key uuid;
BEGIN
    IF p_user_id IS NULL THEN
        RETURN NULL;
    END IF;
    -- Only for accounts that exist. An audit row naming an account that was
    -- already de-identified must not mint a fresh mapping and re-attribute it.
    IF NOT EXISTS (SELECT 1 FROM identity.user_account WHERE id = p_user_id) THEN
        RETURN NULL;
    END IF;
    INSERT INTO identity.account_subject (user_id)
         VALUES (p_user_id)
    ON CONFLICT (user_id) DO NOTHING;
    SELECT subject_key INTO v_key
      FROM identity.account_subject WHERE user_id = p_user_id;
    RETURN v_key;
END;
$$;

ALTER FUNCTION identity.subject_key_for(uuid) OWNER TO onyx_migrator;

COMMENT ON FUNCTION identity.subject_key_for(uuid) IS
    'Entry 11B6A (PD-15). The subject key for a live account, minted on first use. Returns NULL for an account that does not exist, so a de-identified subject is never re-attributed by a later audit write.';

REVOKE ALL ON FUNCTION identity.subject_key_for(uuid) FROM PUBLIC;
-- The audit trigger is SECURITY DEFINER owned by onyx_migrator and calls this
-- as its owner, so no application role needs EXECUTE. Granting it to
-- onyx_app_rw would hand the request path a way to enumerate which accounts
-- exist, one probe at a time.
GRANT EXECUTE ON FUNCTION identity.subject_key_for(uuid) TO onyx_privacy_worker;

-- ---------------------------------------------------------------------------
-- NOT DONE HERE: wiring audit.log_change to call this
-- ---------------------------------------------------------------------------
-- The trigger has to pass the changed row's owner to `subject_key_for` and add
-- the result to its INSERT. That edit belongs inside `audit.log_change`, which
-- lives in 42_audit_payload_minimization.sql and is ~100 lines of payload
-- minimisation, credential redaction and structural-column policy.
--
-- A first attempt reproduced that function here with the subject added and got
-- it wrong: it invented `audit.payload_policy`, where the real code calls
-- `audit.audit_retains_values()` and `audit.audit_structural_columns()`. The
-- schema build failed loudly, which is the good outcome, but the lesson is the
-- durable one — a certified function should be EXTENDED IN PLACE in the file
-- that owns it, not copied into a second file where the copy can drift from
-- the original in ways no test compares.
--
-- So `subject_key` is populated today only by
-- `identity.deidentify_audit_auth`, which sets it for the rows it retains.
-- Ordinary audit writes still record actor and entity and no subject, which
-- means PD-15's separation exists in the schema and is not yet load-bearing in
-- the write path. That is stated as open rather than papered over.
