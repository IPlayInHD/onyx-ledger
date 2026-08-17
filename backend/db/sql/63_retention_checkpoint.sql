-- =============================================================================
-- Entry: Retention / "What Changed?" Engine (migration 0069)
-- =============================================================================
--
-- WHAT THIS IS. The durable record of the product state a user has explicitly
-- ACKNOWLEDGED. "What changed?" is the difference between the latest such
-- checkpoint and current state, so this table is the retention baseline
-- authority and nothing else is.
--
-- WHAT IT IS DELIBERATELY NOT. It is not a last-seen, last-login or
-- last-page-view marker. Those record that a request happened; this records
-- that a person reviewed something and said so. A GET never writes here — if it
-- did, a background refresh could erase changes the user never saw, which is
-- the one failure this design exists to prevent.
--
-- WHY THE SNAPSHOT IS STORED RATHER THAN RECOMPUTED. Comparing against a
-- reconstruction of "what the state probably was" would silently lose exactly
-- the changes worth reporting. The acknowledged bytes are the evidence; current
-- code accommodates them and never rewrites them, which is why the snapshot
-- carries its own schema version independent of every product contract.
--
-- WHAT THE SNAPSHOT MAY NOT CONTAIN. No document id, object key, bucket,
-- content hash or filename; no correlation id; no per-run candidate id; no
-- presentation ordering. Evidence appears only as governed readiness
-- (READY / PARTIAL / MISSING / NOT_REQUIRED / UNKNOWN). See
-- app/services/ioe/retention/snapshot.py, which is the single writer.
--
-- APPEND-ONLY BY GRANTS, following 62_decision_journal's precedent and for the
-- same reason: these rows die with the account through the identity.user_account
-- cascade fired by identity.terminal_remove_account, and that function does not
-- set app.allow_evidence_purge. A GUC-gated DELETE trigger here would abort
-- terminal removal. Revoking UPDATE/DELETE from onyx_app_rw closes the door for
-- the application while referential actions still purge the rows.
--
-- HOW CONCURRENCY IS ENFORCED IN THE DATABASE, not just in the service. Each
-- checkpoint names the one it supersedes, and (user_id, tax_year,
-- supersedes_checkpoint_id) is UNIQUE NULLS NOT DISTINCT. Two tabs that both
-- read baseline C1 and both try to acknowledge cannot both succeed: the second
-- lands on this constraint. NULLS NOT DISTINCT is what extends that guarantee
-- to the first baseline, where the superseded id is NULL and Postgres would
-- otherwise treat every NULL as unique and admit both.

CREATE TABLE ioe.retention_checkpoint (
    id                        uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id                   uuid NOT NULL
                              REFERENCES identity.user_account(id) ON DELETE CASCADE,
    tax_year                  integer NOT NULL,
    -- The snapshot's OWN schema version. Not the lifecycle's, not Assurance's,
    -- not the read contract's: this names the shape of the bytes in `snapshot`,
    -- so a checkpoint written today stays interpretable when those other
    -- contracts move.
    snapshot_schema_version   text NOT NULL,
    snapshot                  jsonb NOT NULL,
    -- Domain-separated hash over the canonical snapshot payload. The
    -- optimistic-concurrency token: a client acknowledging state it never saw
    -- cannot produce this value.
    snapshot_hash             text NOT NULL,
    -- The date that produced the timing bands inside `snapshot`. Recorded
    -- because a band is only meaningful against the date it was evaluated on.
    evaluated_as_of           date NOT NULL,
    -- The checkpoint this one replaces as the baseline. NULL exactly once per
    -- (user, tax_year): the initial baseline. History is preserved — the old
    -- row is never rewritten, only superseded.
    supersedes_checkpoint_id  uuid
                              REFERENCES ioe.retention_checkpoint(id) ON DELETE CASCADE,
    -- Client-supplied idempotency key, matching the decision journal's
    -- convention. A retried acknowledgement lands on this constraint and
    -- returns the checkpoint the first attempt created.
    request_id                uuid NOT NULL,
    acknowledged_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, request_id),
    CONSTRAINT uq_retention_checkpoint_chain
        UNIQUE NULLS NOT DISTINCT (user_id, tax_year, supersedes_checkpoint_id)
);

-- Resolving "the latest acknowledged checkpoint" is the hot path and must not
-- scan a user's history: this index answers it as a single backwards lookup
-- however many checkpoints have accumulated.
CREATE INDEX ix_ioe_retention_checkpoint_latest
    ON ioe.retention_checkpoint (user_id, tax_year, acknowledged_at DESC, id DESC);

COMMENT ON TABLE ioe.retention_checkpoint IS
    'The product state a user explicitly acknowledged, and the baseline every '
    '"what changed?" answer is measured from. Append-only; superseded rather '
    'than rewritten. Not a last-seen marker: only an explicit acknowledgement '
    'writes here. Dies with the account via the user_account cascade '
    '(LIVE_USER_DATA_DELETE).';

-- ---------------------------------------------------------------------------
-- Row-level security: the tenant's own rows, by user_id.
-- ---------------------------------------------------------------------------
ALTER TABLE ioe.retention_checkpoint ENABLE ROW LEVEL SECURITY;
ALTER TABLE ioe.retention_checkpoint FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_retention_checkpoint ON ioe.retention_checkpoint
    USING (user_id = ref.current_app_user())
    WITH CHECK (user_id = ref.current_app_user());

-- ---------------------------------------------------------------------------
-- Privileges. 21_ioe's default privileges granted the application role full
-- DML on new ioe tables; append-only means taking UPDATE and DELETE back.
-- INSERT + SELECT is the entire application surface.
-- ---------------------------------------------------------------------------
REVOKE UPDATE, DELETE ON ioe.retention_checkpoint FROM onyx_app_rw;
