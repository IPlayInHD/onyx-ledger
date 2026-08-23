-- =============================================================================
-- Onyx Ledger — 66 · Durable legal acceptance
-- Alembic revision: 0073_legal_acceptance
--
-- THE RECORD THAT DID NOT EXIST. The sign-up screen has always told customers
-- that creating an account means agreeing to the Terms and the Privacy Policy.
-- Nothing stored that. The frontend said so in as many words — it deliberately
-- shipped NO consent checkbox, on the grounds that a ticked box would create a
-- legal record that exists nowhere. This table is that record.
--
-- WHY NOT audit.consent_log, which already exists and is unused. It is an
-- audit sink and is built like one: the runtime role holds INSERT and nothing
-- else, so a customer could never be shown what they have accepted; there is
-- no unique constraint, so a retried browser request would write duplicate
-- rows forever; there is no RLS; and it carries an `ip_address` column this
-- entry has no justification to fill. It stays exactly where it is and keeps
-- its designed job — the de-identified evidence that OUTLIVES the account, as
-- 50_audit_auth_deidentification.sql already arranges. This table is the live
-- tenant state that answers "may this person use the application", which is a
-- different question needing different access.
--
-- APPEND ONLY, ENFORCED THREE WAYS. A grant that withholds UPDATE and DELETE,
-- triggers that refuse both regardless of grant, and a unique key that makes a
-- second identical acceptance a no-op rather than a duplicate. Belt and braces
-- on purpose: the grant protects against the application, the triggers protect
-- against anything holding a wider role, and the invariant they defend is that
-- an acceptance of version N can never become an acceptance of version N+1.
-- =============================================================================

CREATE TABLE identity.legal_acceptance (
    id               uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),

    -- CASCADE, deliberately, and it is not a loss of evidence. This is live
    -- tenant state: it answers the gate for an account that exists. When the
    -- account is deleted the question stops being asked, and the surviving
    -- proof is the de-identified row in audit.consent_log, which the deletion
    -- pipeline already severs rather than destroys.
    user_id          uuid NOT NULL REFERENCES identity.user_account(id)
                          ON DELETE CASCADE,

    -- The frontend's own route slug: `/legal/terms` renders 'terms'. Not a
    -- parallel code, so a stored acceptance names a document a person can
    -- navigate to. The closed set lives in app/domain/legal.py and is pinned
    -- against this CHECK by a test — the constraint is here as well because a
    -- typo reaching this table would be a permanent, unreadable record.
    document_type    text NOT NULL CHECK (document_type IN (
                          'terms', 'privacy', 'ai-transparency',
                          'tax-disclaimer', 'accessibility', 'security')),

    -- The version string exactly as the registry published it. Not parsed, not
    -- ordered, not compared for precedence: what makes a version current is
    -- the registry saying so, and a database that thought it knew which
    -- version was newer would be a second authority.
    document_version text NOT NULL CHECK (length(document_version) BETWEEN 1 AND 64),

    -- When the customer accepted. Server clock, never a client value.
    accepted_at      timestamptz NOT NULL DEFAULT now(),

    -- NOT STORED, and each omission is a decision:
    --   ip_address   — a network address is personal data, it identifies a
    --                  location rather than an agreement, and no part of this
    --                  product reads one back. B4 §4 says do not collect it
    --                  merely because legal systems often do.
    --   user_agent   — same, and it would be stale evidence of nothing.
    --   document body— the text lives in the frontend where it is rendered;
    --                  a second copy would be two things to keep in step with
    --                  no way to tell which one the customer actually read.

    -- ONE ACCEPTANCE PER PERSON PER DOCUMENT PER VERSION. This is what makes
    -- a retried request idempotent instead of a second row: the service does
    -- ON CONFLICT DO NOTHING against this key, so two tabs, a network retry
    -- and a double-click all converge on the single row that already exists.
    CONSTRAINT uq_legal_acceptance_user_document_version
        UNIQUE (user_id, document_type, document_version)
);

-- The gate reads "everything this user has accepted" on protected requests, so
-- the index that matters is the one covering that lookup. The unique
-- constraint's own index is (user_id, document_type, document_version), which
-- already serves it — this adds the recency ordering an acceptance history
-- screen would want without a second scan.
CREATE INDEX ix_legal_acceptance_user_time
    ON identity.legal_acceptance (user_id, accepted_at DESC);

COMMENT ON TABLE identity.legal_acceptance IS
    'Entry B4. Durable proof that one account accepted one version of one legal document. Append-only: no UPDATE, no DELETE, unique per (user, document, version). Live tenant state — the evidence that outlives the account is the de-identified row in audit.consent_log.';

-- ---- Row-level security ------------------------------------------------------
-- ENABLE and FORCE both. Without FORCE the policies do not apply to the table
-- owner, so the table would look protected while the owning role read every
-- tenant's legal history.
ALTER TABLE identity.legal_acceptance ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity.legal_acceptance FORCE ROW LEVEL SECURITY;

-- USING controls what is visible; WITH CHECK controls what may be written. A
-- policy with only USING would let a caller INSERT an acceptance for another
-- account that it could then never see — writing consent in somebody else's
-- name, invisibly.
--
-- Unlike the recovery-token tables, self-ownership RLS is exactly right here:
-- every path that touches this table is AUTHENTICATED, so `app.user_id` is
-- always set. There is no anonymous caller to lock out.
CREATE POLICY p_self_legal_acceptance ON identity.legal_acceptance
    USING (user_id = ref.current_app_user())
    WITH CHECK (user_id = ref.current_app_user());

-- ---- Privileges --------------------------------------------------------------
-- The blanket `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA
-- identity` in 16_rls_grants.sql runs before this file and does not reach a
-- table that does not exist yet — but ALTER DEFAULT PRIVILEGES in that same
-- file does, so the four arrive anyway. Revoking first is what makes the
-- grant below the real answer rather than a subset of a wider one.
REVOKE ALL ON identity.legal_acceptance FROM onyx_app_rw, onyx_app_ro;

-- INSERT and SELECT, and nothing else. The workflow is "record an acceptance"
-- and "tell the customer what they have accepted"; neither needs UPDATE and
-- neither needs DELETE, so the runtime does not hold them. An append-only
-- table whose writer can UPDATE is append-only by convention.
GRANT SELECT, INSERT ON identity.legal_acceptance TO onyx_app_rw;

-- NOTHING for the reporting role, deliberately, and this is the one place B4
-- declines to repeat an existing pattern. Every other identity table is
-- readable by onyx_app_ro through a blanket grant; B3 recorded that as a
-- modest privacy leak for the recovery-token tables. Legal history is a record
-- of what a named person agreed to and when. A reporting role gets it when
-- somebody names the report, not by inheriting a wildcard.

-- ---- Immutability ------------------------------------------------------------
-- The grant already withholds UPDATE and DELETE from the runtime. These
-- triggers refuse them from ANY role, which is the difference between "the
-- application cannot rewrite history" and "history cannot be rewritten".
--
-- The invariant, stated plainly: an acceptance of version N must never become
-- an acceptance of version N+1. Terms v2 replacing v1 produces a SECOND row.
-- The history is "accepted v1 at T1, accepted v2 at T2" — which is the true
-- account of what happened, and the only one worth keeping.
CREATE OR REPLACE FUNCTION identity.reject_legal_acceptance_mutation()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION
        'legal acceptance rows are append-only; % is not permitted', TG_OP
        USING ERRCODE = 'check_violation';
END;
$$;

ALTER FUNCTION identity.reject_legal_acceptance_mutation() OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION identity.reject_legal_acceptance_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_legal_acceptance_no_update ON identity.legal_acceptance;
CREATE TRIGGER trg_legal_acceptance_no_update
    BEFORE UPDATE ON identity.legal_acceptance
    FOR EACH ROW EXECUTE FUNCTION identity.reject_legal_acceptance_mutation();

-- The account cascade is the ONE deletion that must still work: it is the
-- database removing a child whose parent is gone, and blocking it would make
-- account deletion impossible. A statement-level trigger fires for a direct
-- `DELETE FROM identity.legal_acceptance` and does not fire for the cascade,
-- which is exactly the distinction wanted here.
DROP TRIGGER IF EXISTS trg_legal_acceptance_no_delete ON identity.legal_acceptance;
CREATE TRIGGER trg_legal_acceptance_no_delete
    BEFORE DELETE ON identity.legal_acceptance
    FOR EACH STATEMENT EXECUTE FUNCTION identity.reject_legal_acceptance_mutation();

-- NO BACKFILL. Not one row is written here for an account that existed before
-- this migration, and that is the most important line in the file. There is no
-- durable record of what anybody accepted before today, so any row this
-- migration invented would be the software fabricating consent — a false
-- statement in the one table whose entire purpose is to be true. Existing
-- accounts have outstanding acceptance and are asked on their next session.
