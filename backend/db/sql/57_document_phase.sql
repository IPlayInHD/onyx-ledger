-- =============================================================================
-- Entry 11B6F — the DOCUMENTS account-level deletion phase (migration 0063)
-- =============================================================================
--
-- `DocumentProcessingService.delete_document` has been THE authoritative
-- document-deletion operation since Entry 11B4: it deletes the binary first,
-- then the extraction rows, then tombstones the document, keeping the content
-- hash and the opaque object key. What has never existed is anything that runs
-- it for every document of an account being deleted. `DOCUMENTS` was named in
-- the phase CHECK constraint and in `SourceDataPhase`, and nothing drove it.
--
-- WHY THE DB HALF IS SPLIT INTO TWO VERBS. The certified ordering is storage
-- first: the binary is the irreversible part with no transaction to roll back,
-- so it goes before any database change, and a crash between them leaves a live
-- row whose retry converges. A single SQL keyhole that tombstoned everything
-- would have to run before or after the whole storage pass and would break that
-- per-document ordering. So the worker asks which documents are outstanding,
-- deletes each binary through the storage port, and finalises each one in the
-- database only once its binary is gone.
--
-- WHY KEYHOLES AT ALL. `onyx_privacy_worker` holds no DELETE on
-- `docs.document_extraction` or `docs.extraction_field` and no UPDATE on
-- `docs.document`, which is correct — the role that can erase one account's
-- documents must not be able to express "every account's". Both verbs are
-- account-scoped and claim-authorised, exactly like `purge_source_data`.
--
-- WHAT IS DELIBERATELY KEPT, per Entry 11A §8 and unchanged here:
--     content_hash    proves WHICH document was deleted
--     object_key      opaque since 11B4; distinguishes deleted-on-purpose
--                     from vanished
--     document_link   the provenance edge, so a confirmed figure never looks
--                     unsourced
--     confirmed facts the user asserted them; they are the tax input

-- ---------------------------------------------------------------------------
-- 1. What is still outstanding
-- ---------------------------------------------------------------------------
-- Live document rows, plus any extraction rows that survived one. A tombstoned
-- document is NOT counted: its binary is gone and its extraction rows were
-- purged in the same call, and the row that remains is the deletion record.
--
-- THE STORAGE HALF IS COUNTED BY THIS TOO, without reaching for the provider.
-- `delete_document` tombstones only after the binary is confirmed gone, so a
-- row with `deleted_at` set is a durable record that the object went with it.
-- Zero live rows therefore means zero objects left behind — which is the whole
-- reason the ordering is storage-first.
CREATE OR REPLACE FUNCTION identity.count_remaining_document_privacy_work(
    p_user_id uuid
)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = docs, pg_catalog
AS $$
    SELECT (
        (SELECT count(*) FROM docs.document
          WHERE user_id = p_user_id AND deleted_at IS NULL)
      + (SELECT count(*) FROM docs.document_extraction e
          JOIN docs.document d ON d.id = e.document_id
         WHERE d.user_id = p_user_id)
      + (SELECT count(*) FROM docs.extraction_field f
          JOIN docs.document_extraction e ON e.id = f.extraction_id
          JOIN docs.document d ON d.id = e.document_id
         WHERE d.user_id = p_user_id)
    )::integer
$$;

ALTER FUNCTION identity.count_remaining_document_privacy_work(uuid)
    OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION identity.count_remaining_document_privacy_work(uuid)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.count_remaining_document_privacy_work(uuid)
    TO onyx_privacy_worker;

COMMENT ON FUNCTION identity.count_remaining_document_privacy_work(uuid) IS
    'Entry 11B6F. Document work still owed before terminal account removal: live document rows plus any surviving extraction rows. Tombstones are not counted — they are the deletion record, and because the binary is deleted before the tombstone is written, zero live rows also means zero objects left in storage.';

-- ---------------------------------------------------------------------------
-- 2. Which documents still need their binary removed
-- ---------------------------------------------------------------------------
-- Returns the opaque storage coordinates and nothing else: no filename (the
-- schema stores none), no mime type, no size, no hash. The worker learns where
-- the object is and not what it was.
CREATE OR REPLACE FUNCTION identity.list_account_documents_for_purge(
    p_user_id     uuid,
    p_claim_token uuid,
    p_worker_id   text,
    p_limit       integer DEFAULT 500
)
RETURNS TABLE (out_document_id uuid, out_bucket text, out_object_key text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = identity, docs, pg_catalog
AS $$
BEGIN
    PERFORM 1 FROM identity.account_lifecycle
     WHERE user_id = p_user_id
       AND claim_token = p_claim_token
       AND claimed_by = p_worker_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no live claim for this subject'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    RETURN QUERY
        SELECT d.id, d.bucket, d.object_key
          FROM docs.document d
         WHERE d.user_id = p_user_id AND d.deleted_at IS NULL
         ORDER BY d.id
         LIMIT least(greatest(coalesce(p_limit, 1), 1), 2000);
END;
$$;

ALTER FUNCTION identity.list_account_documents_for_purge(uuid, uuid, text, integer)
    OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION identity.list_account_documents_for_purge(uuid, uuid, text, integer)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.list_account_documents_for_purge(uuid, uuid, text, integer)
    TO onyx_privacy_worker;

-- ---------------------------------------------------------------------------
-- 3. Finalise one document, after its binary is gone
-- ---------------------------------------------------------------------------
-- Purges the extraction rows and writes the tombstone, in one transaction and
-- one document at a time. Called only once the storage delete has returned
-- DELETED or ALREADY_ABSENT, so the tombstone never claims more than happened.
--
-- Idempotent: a document that is already tombstoned returns 0 and changes
-- nothing, which is what makes the retry after a mid-pass crash converge.
CREATE OR REPLACE FUNCTION identity.finalize_document_purge(
    p_user_id     uuid,
    p_document_id uuid,
    p_claim_token uuid,
    p_worker_id   text
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, docs, pg_catalog
AS $$
DECLARE
    v_fields      integer := 0;
    v_extractions integer := 0;
BEGIN
    PERFORM 1 FROM identity.account_lifecycle
     WHERE user_id = p_user_id
       AND claim_token = p_claim_token
       AND claimed_by = p_worker_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no live claim for this subject'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- Ownership is checked here rather than trusted from the caller: the
    -- worker passes a document id, and nothing else stops it passing somebody
    -- else's.
    PERFORM 1 FROM docs.document
     WHERE id = p_document_id AND user_id = p_user_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 0;
    END IF;

    WITH gone AS (
        DELETE FROM docs.extraction_field f
         USING docs.document_extraction e
         WHERE f.extraction_id = e.id AND e.document_id = p_document_id
        RETURNING 1)
    SELECT count(*) INTO v_fields FROM gone;

    WITH gone AS (
        DELETE FROM docs.document_extraction
         WHERE document_id = p_document_id
        RETURNING 1)
    SELECT count(*) INTO v_extractions FROM gone;

    UPDATE docs.document
       SET deleted_at = now(), updated_at = now()
     WHERE id = p_document_id AND deleted_at IS NULL;

    RETURN v_fields + v_extractions;
END;
$$;

ALTER FUNCTION identity.finalize_document_purge(uuid, uuid, uuid, text)
    OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION identity.finalize_document_purge(uuid, uuid, uuid, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION identity.finalize_document_purge(uuid, uuid, uuid, text)
    FROM onyx_app_rw, onyx_app_ro;
GRANT EXECUTE ON FUNCTION identity.finalize_document_purge(uuid, uuid, uuid, text)
    TO onyx_privacy_worker;

COMMENT ON FUNCTION identity.finalize_document_purge(uuid, uuid, uuid, text) IS
    'Entry 11B6F. Purges one document''s extraction rows and writes its tombstone, after its binary has been deleted. Account-scoped and claim-authorised; idempotent on an already-tombstoned document. content_hash, object_key and the provenance edge in docs.document_link are deliberately kept.';

-- ---------------------------------------------------------------------------
-- 4. Completion refuses while work remains
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION identity.complete_lifecycle_phase(
    p_user_id uuid, p_phase text, p_claim_token uuid, p_worker_id text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = identity, pg_catalog
AS $$
DECLARE
    v_remaining bigint;
BEGIN
    PERFORM 1 FROM identity.account_lifecycle
      WHERE user_id = p_user_id AND claim_token = p_claim_token FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF p_phase = 'SOURCE_DATA' THEN
        v_remaining := identity.count_remaining_source_data(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'SOURCE_DATA cannot complete: % in-scope rows remain', v_remaining;
        END IF;
    END IF;

    IF p_phase = 'AUDIT_AUTH_DEIDENTIFICATION' THEN
        v_remaining := identity.count_attributable_audit_auth(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'AUDIT_AUTH_DEIDENTIFICATION cannot complete: % attributable rows remain',
                v_remaining;
        END IF;
    END IF;

    IF p_phase = 'SCENARIO_RETENTION' THEN
        v_remaining := identity.count_remaining_scenario_privacy_work(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'SCENARIO_RETENTION cannot complete: % scenario rows remain', v_remaining;
        END IF;
    END IF;

    IF p_phase = 'DOCUMENTS' THEN
        v_remaining := identity.count_remaining_document_privacy_work(p_user_id);
        IF v_remaining <> 0 THEN
            RAISE EXCEPTION
                'DOCUMENTS cannot complete: % document rows remain', v_remaining;
        END IF;
    END IF;

    UPDATE identity.account_lifecycle_phase
       SET status = 'COMPLETE', completed_at = now(),
           last_failure_code = NULL, updated_at = now()
     WHERE user_id = p_user_id AND phase = p_phase;
    RETURN FOUND;
END;
$$;
