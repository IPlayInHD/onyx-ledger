-- Entry 11B6B — make subject severance irreversible, and use ONE subject key
--
-- Two defects in the Entry 11B6 mechanism, both found by testing it rather
-- than by reading it. Neither ever ran in production — the phase was never
-- wired — but both are in the schema at revision 0056.
--
-- DEFECT 1: SEVERANCE WAS REVERSIBLE. `identity.deletion_subject` was keyed by
-- `user_id`, so after de-identification this join brought the severed
-- correlation straight back:
--
--     SELECT a.actor_id, d.subject_key
--       FROM audit.audit_log a
--       JOIN identity.deletion_subject d ON d.user_id = a.actor_id;
--
-- The former account UUID survives in immutable audit rows as `actor_id`, so
-- ANY table mapping that UUID to the retained subject key undoes the whole
-- exercise. The tombstone kept `user_id` to make retries idempotent, and
-- idempotency is not a good enough reason to keep a re-identification map.
--
-- DEFECT 2: TWO KEY SPACES. `deletion_subject.subject_key` had its own
-- DEFAULT, so it minted a key unrelated to the one `identity.account_subject`
-- had already given the account. Audit rows carried one key and login events
-- were stamped with another, so one person's retained history was split across
-- two unrelated identifiers — exactly the correlation the key exists to
-- provide. There is now one subject key per account, minted once by
-- `account_subject`, and `deletion_subject` records that it has been retired.
--
-- IDEMPOTENCY WITHOUT REVERSIBILITY. The first run finds a live mapping,
-- stamps the retained rows, records the key as retired and removes the
-- mapping. A second run finds no mapping and returns zeros — already
-- de-identified. Nothing needs a lookup from the old account id to do that.

-- ---------------------------------------------------------------------------
-- The tombstone, keyed by the subject and not by the account
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS identity.deletion_subject;

CREATE TABLE identity.deletion_subject (
    subject_key uuid PRIMARY KEY,
    retired_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE identity.deletion_subject IS
    'Entry 11B6B. Records that a subject key belongs to a de-identified account. Deliberately carries NO account id: a column mapping the former UUID to the retained key would make severance reversible by a single join, which is the defect this table shape exists to prevent.';

ALTER TABLE identity.deletion_subject ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity.deletion_subject FORCE ROW LEVEL SECURITY;
REVOKE ALL ON identity.deletion_subject FROM PUBLIC;
REVOKE ALL ON identity.deletion_subject FROM onyx_app_rw, onyx_app_ro;

-- ---------------------------------------------------------------------------
-- The keyhole, on one key space
-- ---------------------------------------------------------------------------
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
    PERFORM 1 FROM identity.account_lifecycle
     WHERE user_id = p_user_id
       AND claim_token = p_claim_token
       AND claimed_by = p_worker_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no live claim for this subject'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- THE ONE subject key, the one audit rows already carry.
    SELECT subject_key INTO v_subject
      FROM identity.account_subject WHERE user_id = p_user_id;

    IF v_subject IS NULL THEN
        -- Already de-identified. Idempotency without a reverse lookup: the
        -- absence of the mapping IS the record that this has been done.
        RETURN QUERY SELECT NULL::uuid, 0, 0, 0, 0;
        RETURN;
    END IF;

    WITH touched AS (
        UPDATE identity.login_event
           SET email_tried = NULL,
               user_agent = NULL,
               ip_address = identity.coarsen_ip(ip_address),
               user_id = NULL,
               subject_key = v_subject,
               deidentified_at = now()
         WHERE (user_id = p_user_id
                -- The anonymous case: a failed login against a non-existent
                -- account has user_id NULL and names the person only by the
                -- email someone typed. Inline rather than through a typed
                -- variable, because `citext` is not on this function's
                -- search_path and does not need to be.
                OR email_tried = (SELECT email FROM identity.user_account
                                   WHERE id = p_user_id))
           AND deidentified_at IS NULL
        RETURNING 1)
    SELECT count(*) INTO v_login FROM touched;

    -- Audit rows are append-only and are NOT touched. Counting them is
    -- reporting, not mutation.
    SELECT count(*) INTO v_audit
      FROM audit.audit_log WHERE subject_key = v_subject;

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

    -- Retire the key, then remove what resolves it. Order matters only for
    -- readability; both are in one transaction.
    INSERT INTO identity.deletion_subject (subject_key)
         VALUES (v_subject)
    ON CONFLICT (subject_key) DO NOTHING;
    DELETE FROM identity.account_subject WHERE user_id = p_user_id;

    RETURN QUERY SELECT v_subject, v_login, v_audit, v_sec, v_consent;
END;
$$;

ALTER FUNCTION identity.deidentify_audit_auth(uuid, uuid, text)
    OWNER TO onyx_migrator;

REVOKE ALL ON FUNCTION identity.deidentify_audit_auth(uuid, uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.deidentify_audit_auth(uuid, uuid, text)
    FROM onyx_app_rw, onyx_app_ro;
GRANT EXECUTE ON FUNCTION identity.deidentify_audit_auth(uuid, uuid, text)
    TO onyx_privacy_worker;

-- ---------------------------------------------------------------------------
-- The completion guard, on the same key space
-- ---------------------------------------------------------------------------
-- Counts ATTRIBUTION MATERIAL, not surviving evidence. An immutable audit row
-- whose subject key no longer resolves is retained history, not a violation,
-- and is deliberately not counted.
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
      + (SELECT count(*) FROM audit.security_event WHERE user_id = p_user_id)
      + (SELECT count(*) FROM audit.consent_log WHERE user_id = p_user_id)
      -- The live mapping itself is attribution material: while it exists the
      -- account resolves to its retained history.
      + (SELECT count(*) FROM identity.account_subject WHERE user_id = p_user_id)
    )::integer
$$;

ALTER FUNCTION identity.count_attributable_audit_auth(uuid) OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION identity.count_attributable_audit_auth(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.count_attributable_audit_auth(uuid)
    TO onyx_privacy_worker;
