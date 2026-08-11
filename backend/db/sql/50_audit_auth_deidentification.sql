-- Entry 11B6 — audit & authentication de-identification (PD-15, PD-3)
--
-- WHAT THIS IS FOR. After an account is deleted, its security and audit
-- history has to keep being useful — a failed-login burst is evidence whether
-- or not the account it targeted still exists — while stopping being
-- attributable to a person. Those two requirements pull in opposite
-- directions, so nothing here deletes an event. It removes identity FROM
-- events and leaves the events themselves standing.
--
-- PD-15. `audit.audit_log` records who ACTED (`actor_id`) and never who the
-- action was ABOUT. An operator disabling a customer's account writes the
-- OPERATOR there, so a de-identification keyed on `actor_id` would rewrite the
-- operator's accountability and miss the customer entirely; an anonymous
-- registration writes NULL and leaves the person only in the payload. This
-- file adds the missing half — a subject — so actor and subject stop being one
-- overloaded column.
--
-- PD-3. `identity.login_event.email_tried` is plaintext, and on a failed login
-- against a non-existent account `user_id` is NULL, so no account-keyed
-- deletion can ever reach it. That row is the reason this phase cannot simply
-- follow foreign keys.
--
-- WHY A RANDOM SUBJECT KEY AND NOT A HASH. A key derived from the email or
-- from the account id would re-link a person who re-registers with the same
-- address, which is precisely what must not happen, and a digest of a
-- low-entropy identifier like an email address is not a pseudonym — it is a
-- lookup table away from the original. The key here is random
-- (`ref.uuid_generate_v7`), minted once per deleted account, and derived from
-- nothing about the person.
--
-- WHAT IS DELIBERATELY KEPT. The mapping row itself keeps `user_id`. That is
-- what makes the phase idempotent — a retry finds the existing key instead of
-- minting a second one — and after deletion the account it names no longer
-- exists, so it is a tombstone rather than a handle. Re-registration mints a
-- new account id and therefore cannot reach the old subject.

-- ---------------------------------------------------------------------------
-- The deletion-safe subject
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS identity.deletion_subject (
    user_id      uuid PRIMARY KEY,
    subject_key  uuid NOT NULL UNIQUE DEFAULT ref.uuid_generate_v7(),
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- NO foreign key to identity.user_account, for the same reason the PD-9
-- deletion ledger has none: this record must outlive the account it describes.
-- A cascade here would erase the mapping at the moment it becomes load-bearing.

COMMENT ON TABLE identity.deletion_subject IS
    'Entry 11B6. Maps a deleted account to a random, non-derived subject key so retained audit and authentication events can be correlated with each other without being attributable to a person. Not derived from email or from any profile value; re-registration mints a new account id and therefore a new subject.';

ALTER TABLE identity.deletion_subject ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity.deletion_subject FORCE ROW LEVEL SECURITY;

REVOKE ALL ON identity.deletion_subject FROM PUBLIC;
REVOKE ALL ON identity.deletion_subject FROM onyx_app_rw, onyx_app_ro;
-- No grant to any application role at all. The only reader and writer is the
-- SECURITY DEFINER function below, which runs as the table's owner.

-- ---------------------------------------------------------------------------
-- PD-15: audit rows gain a subject, distinct from the actor
-- ---------------------------------------------------------------------------
-- AUDIT ROWS ARE NEVER REWRITTEN. `audit.audit_log` carries an append-only
-- trigger that refuses UPDATE even for the table owner, and that immutability
-- is the point of an audit log — a de-identification pass that could edit it
-- could also edit it dishonestly. So the subject is recorded as a KEY from the
-- start, and attribution is broken by removing what the key resolves to.
--
-- This is why `subject_key` and not `subject_user_id`: there is nothing to
-- clear at deletion time, and nothing to get half-cleared by a crash.
ALTER TABLE audit.audit_log
    ADD COLUMN IF NOT EXISTS subject_key uuid;

COMMENT ON COLUMN audit.audit_log.subject_key IS
    'Entry 11B6 (PD-15). The account an action CONCERNED, as a key rather than an id — the actor is a different column and stays. Resolves through identity.account_subject while the account lives; after deletion that mapping is removed and the key resolves to nothing, so the row stays readable as evidence and stops naming a person. The audit log is append-only and is never rewritten.';

ALTER TABLE audit.security_event
    ADD COLUMN IF NOT EXISTS subject_key uuid;
ALTER TABLE audit.consent_log
    ADD COLUMN IF NOT EXISTS subject_key uuid;
ALTER TABLE identity.login_event
    ADD COLUMN IF NOT EXISTS subject_key uuid;

-- PD-3: a de-identified login event records that an email WAS tried without
-- recording which. The event, its type and its timing are the security value.
ALTER TABLE identity.login_event
    ADD COLUMN IF NOT EXISTS deidentified_at timestamptz;

COMMENT ON COLUMN identity.login_event.deidentified_at IS
    'Entry 11B6 (PD-3). When identity was removed from this row. Non-NULL implies email_tried IS NULL and ip_address has been coarsened; the guard in identity.assert_login_events_deidentified checks exactly that.';

-- The live half of the subject mapping. `deletion_subject` is the tombstone;
-- this is what resolves a key while the account exists. Deleting one row here
-- is what makes every audit row carrying that key non-attributable, without
-- touching a single audit row.
CREATE TABLE IF NOT EXISTS identity.account_subject (
    user_id     uuid PRIMARY KEY,
    subject_key uuid NOT NULL UNIQUE DEFAULT ref.uuid_generate_v7(),
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE identity.account_subject IS
    'Entry 11B6 (PD-15). Resolves audit.audit_log.subject_key to a live account. Removed by the de-identification phase, which is how append-only audit history stops being attributable without being edited.';

ALTER TABLE identity.account_subject ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity.account_subject FORCE ROW LEVEL SECURITY;
REVOKE ALL ON identity.account_subject FROM PUBLIC;
REVOKE ALL ON identity.account_subject FROM onyx_app_rw, onyx_app_ro;

-- ---------------------------------------------------------------------------
-- IP coarsening
-- ---------------------------------------------------------------------------
-- Keeps the network neighbourhood — enough to see a burst from one source —
-- and drops the host. /24 for IPv4 and /48 for IPv6 are the conventional
-- boundaries for that trade. HOW LONG a full address may be kept before this
-- runs is a retention question this file does not answer: the mechanism is
-- here, the duration is POLICY_DECISION_REQUIRED.
CREATE OR REPLACE FUNCTION identity.coarsen_ip(p_ip inet)
RETURNS inet
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog
AS $$
    SELECT CASE
        WHEN p_ip IS NULL THEN NULL
        WHEN family(p_ip) = 4 THEN set_masklen(network(set_masklen(p_ip, 24)), 24)
        ELSE set_masklen(network(set_masklen(p_ip, 48)), 48)
    END
$$;

COMMENT ON FUNCTION identity.coarsen_ip(inet) IS
    'Entry 11B6 (PD-3). Reduces an address to its /24 (IPv4) or /48 (IPv6) network, preserving burst-from-one-source evidence while dropping the host.';

-- ---------------------------------------------------------------------------
-- The keyhole
-- ---------------------------------------------------------------------------
-- One subject per call, chosen by the caller, verified against a live claim.
-- The worker cannot express "every account": there is no unbounded form of
-- this function, which is the same shape identity.purge_source_data has.
CREATE OR REPLACE FUNCTION identity.deidentify_audit_auth(
    p_user_id     uuid,
    p_claim_token uuid,
    p_worker_id   text
)
RETURNS TABLE (out_subject_key uuid, out_login_events integer,
               out_audit_rows integer, out_security_events integer,
               out_consent_rows integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, audit, ref, pg_catalog
AS $$
DECLARE
    v_subject uuid;
    v_login   integer := 0;
    v_audit   integer := 0;
    v_sec     integer := 0;
    v_consent integer := 0;
BEGIN
    -- The claim is the authorization. Without this a worker holding no lease
    -- could de-identify any account, which is the lifecycle-ACL defect Entry
    -- 11B5J found in the other direction.
    PERFORM 1 FROM identity.account_lifecycle
     WHERE user_id = p_user_id
       AND claim_token = p_claim_token
       AND claimed_by = p_worker_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no live claim for this subject'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- Idempotent: one subject key per account, minted once. A retry after a
    -- crash mid-phase finds the existing row rather than issuing a second key
    -- and splitting one person's history in two.
    INSERT INTO identity.deletion_subject (user_id)
         VALUES (p_user_id)
    ON CONFLICT (user_id) DO NOTHING;
    SELECT subject_key INTO v_subject
      FROM identity.deletion_subject WHERE user_id = p_user_id;

    -- PD-3. Both halves of the problem: rows the account owns, and rows that
    -- only ever named it by the email someone typed. The second is why this
    -- cannot be a foreign-key cascade.
    WITH touched AS (
        UPDATE identity.login_event
           SET email_tried = NULL,
               user_agent = NULL,
               ip_address = identity.coarsen_ip(ip_address),
               user_id = NULL,
               subject_key = v_subject,
               deidentified_at = now()
         WHERE (user_id = p_user_id
                OR email_tried = (SELECT email FROM identity.user_account
                                   WHERE id = p_user_id))
           AND deidentified_at IS NULL
        RETURNING 1)
    SELECT count(*) INTO v_login FROM touched;

    -- PD-15. NOT an UPDATE: audit.audit_log is append-only and refuses one.
    -- Removing the mapping makes every audit row carrying this account's
    -- subject key resolve to nothing, which de-identifies the whole history at
    -- once and leaves the evidence itself byte-identical. The ACTOR column is
    -- never touched, so an operator's accountability survives the account.
    SELECT count(*) INTO v_audit
      FROM audit.audit_log a
      JOIN identity.account_subject s ON s.subject_key = a.subject_key
     WHERE s.user_id = p_user_id;
    DELETE FROM identity.account_subject WHERE user_id = p_user_id;

    WITH touched AS (
        UPDATE audit.security_event
           SET user_id = NULL,
               subject_key = v_subject,
               ip_address = identity.coarsen_ip(ip_address)
         WHERE user_id = p_user_id
        RETURNING 1)
    SELECT count(*) INTO v_sec FROM touched;

    WITH touched AS (
        UPDATE audit.consent_log
           SET user_id = NULL,
               subject_key = v_subject,
               ip_address = identity.coarsen_ip(ip_address)
         WHERE user_id = p_user_id
        RETURNING 1)
    SELECT count(*) INTO v_consent FROM touched;

    RETURN QUERY SELECT v_subject, v_login, v_audit, v_sec, v_consent;
END;
$$;

ALTER FUNCTION identity.deidentify_audit_auth(uuid, uuid, text)
    OWNER TO onyx_migrator;

COMMENT ON FUNCTION identity.deidentify_audit_auth(uuid, uuid, text) IS
    'Entry 11B6 (PD-15, PD-3). De-identifies one subject''s audit and authentication history under a verified lifecycle claim. Removes identity from events; deletes none. Rewrites the SUBJECT and never the ACTOR, so operator accountability survives the account.';

REVOKE ALL ON FUNCTION identity.deidentify_audit_auth(uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.deidentify_audit_auth(uuid, uuid, text) FROM onyx_app_rw, onyx_app_ro;
GRANT EXECUTE ON FUNCTION identity.deidentify_audit_auth(uuid, uuid, text)
    TO onyx_privacy_worker;

-- ---------------------------------------------------------------------------
-- The completion guard
-- ---------------------------------------------------------------------------
-- A phase that reported COMPLETE while attributable rows remained would be a
-- status that lies in the direction that matters. The worker calls this before
-- completing, and it counts what is still attributable rather than trusting
-- the update's own return value.
CREATE OR REPLACE FUNCTION identity.count_attributable_audit_auth(p_user_id uuid)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = identity, audit, pg_catalog
AS $$
    SELECT (
        (SELECT count(*) FROM identity.login_event
          WHERE user_id = p_user_id
             OR (email_tried IS NOT NULL
                 AND email_tried = (SELECT email FROM identity.user_account
                                     WHERE id = p_user_id)))
      + (SELECT count(*) FROM audit.audit_log a
           JOIN identity.account_subject s ON s.subject_key = a.subject_key
          WHERE s.user_id = p_user_id)
      + (SELECT count(*) FROM audit.security_event WHERE user_id = p_user_id)
      + (SELECT count(*) FROM audit.consent_log WHERE user_id = p_user_id)
    )::integer
$$;

ALTER FUNCTION identity.count_attributable_audit_auth(uuid) OWNER TO onyx_migrator;

COMMENT ON FUNCTION identity.count_attributable_audit_auth(uuid) IS
    'Entry 11B6. Rows still attributable to this account across audit and authentication surfaces. The AUDIT_AUTH_DEIDENTIFICATION phase refuses to complete while this is non-zero.';

REVOKE ALL ON FUNCTION identity.count_attributable_audit_auth(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.count_attributable_audit_auth(uuid)
    TO onyx_privacy_worker;
