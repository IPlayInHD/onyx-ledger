-- =============================================================================
-- Onyx Ledger — 44 · PD-9 durable deletion ledger  (schemas: identity, audit)
-- Alembic revision: 0050_pd9_durable_deletion_ledger
--
-- Entry 11B3. PD-9, as recorded in the Entry 11A gap register:
--
--     `audit.data_deletion_request` FK is `CASCADE`, so the deletion record
--     dies with the account it must outlive.
--
-- THE WORDING NAMES THE WRONG TABLE, AND THE DEFECT IS REAL
-- Entry 11A wrote that when `audit.data_deletion_request` was the presumed home
-- for a deletion ledger. It described that table as "EXISTS AND IS UNUSED" and
-- it still holds zero rows. Entry 11B2 then built the real lifecycle somewhere
-- else — `identity.account_lifecycle` — and reproduced the same mistake in a
-- stronger form: there the account id is not merely a cascading foreign key, it
-- is the PRIMARY KEY.
--
-- WHAT ACTUALLY HAPPENS TODAY, MEASURED RATHER THAN ASSUMED
-- Not silent loss. `DELETE FROM identity.user_account` FAILS:
--
--     ERROR: account lifecycle rows are not deletable
--     CONTEXT: PL/pgSQL function identity.reject_lifecycle_delete()
--     SQL statement: DELETE FROM ONLY "identity"."account_lifecycle" ...
--
-- The cascade tries to destroy the ledger and Entry 11B2's own no-delete
-- trigger refuses. So the evidence is safe and the ACCOUNT CAN NEVER BE
-- REMOVED while a lifecycle exists — which blocks the last step of every purge
-- phase this entry is a prerequisite for. Two wrongs that happened to cancel
-- into a third.
--
-- On `audit.data_deletion_request` there is no such trigger, so the cascade
-- there WOULD silently destroy the record. That trap is unarmed only because
-- nothing writes to the table.
--
-- THE FIX: SEVER THE LINK, KEEP THE SUBJECT
-- The account's own UUID is already the durable subject identifier. It is
-- internal, immutable, never reused, and it is what a restored backup's
-- `user_account` rows will carry — so a ledger keyed on it identifies exactly
-- which restored accounts are owed a re-deletion. Nothing needs to be invented:
--
--   * NO pseudonymous digest. There is no plaintext to hash, no key to manage,
--     and no new cryptography to get wrong. Entry 11B3 §5 asks that the
--     existing immutable internal UUID be evaluated first; it is sufficient.
--   * NO surrogate primary key. `user_id` as the PK is what makes "one logical
--     lifecycle per account" a database fact rather than application logic, and
--     Entry 11B2's idempotency — twenty repeated requests, ten concurrent ones,
--     one row — rests on exactly that. A surrogate key would have to be
--     defended by a separate unique constraint that says the same thing.
--   * NO retained account fields. No email, no name, no status. The ledger
--     keeps a subject identifier and its own machinery, nothing about the
--     person.
--
-- WHAT REPLACES THE FOREIGN KEY
-- The FK was doing two jobs. Preventing rows for accounts that never existed is
-- worth keeping; destroying the ledger when the account goes is the defect. So
-- the check moves to the only moment it is meaningful — creation — and stops
-- applying afterwards, which is precisely the asymmetry a foreign key cannot
-- express.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1 · Sever the cascade on the live ledger
-- ---------------------------------------------------------------------------
ALTER TABLE identity.account_lifecycle
    DROP CONSTRAINT IF EXISTS account_lifecycle_user_id_fkey;

COMMENT ON COLUMN identity.account_lifecycle.user_id IS
  'The DURABLE SUBJECT of the deletion. The account''s own internal UUID: immutable, never reused, and deliberately NOT a foreign key — this record must outlive the account row it describes so a purge can finish and a restored backup can be told what to re-delete. Validated against a live account at INSERT time only, by trg_account_lifecycle_subject_exists.';

-- ---------------------------------------------------------------------------
-- 2 · Integrity at the only moment it still means something
--
-- A foreign key would re-arm the cascade. This gives the same guarantee for
-- creation — you cannot open a lifecycle for an account that does not exist —
-- and deliberately says nothing about what happens later, which is the whole
-- point.
--
-- INSERT only. No UPDATE branch: `user_id` is the primary key and Entry 11B2's
-- transition trigger already rejects any attempt to change it, so a row cannot
-- be re-pointed at a different subject.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION identity.require_lifecycle_subject_exists()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = identity, pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM identity.user_account WHERE id = NEW.user_id
    ) THEN
        RAISE EXCEPTION
            'account lifecycle subject does not exist'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION identity.require_lifecycle_subject_exists() IS
  'Replaces the creation half of the foreign key PD-9 removed: a lifecycle may only be opened for an account that exists. Says nothing about deletion, because the record must outlive the account.';

DROP TRIGGER IF EXISTS trg_account_lifecycle_subject_exists
    ON identity.account_lifecycle;
CREATE TRIGGER trg_account_lifecycle_subject_exists
    BEFORE INSERT ON identity.account_lifecycle
    FOR EACH ROW EXECUTE FUNCTION identity.require_lifecycle_subject_exists();

-- ---------------------------------------------------------------------------
-- 3 · The table PD-9's wording actually names
--
-- Unused and empty, and fixed anyway. It is the table a future author would
-- reach for when asked where deletion requests live, and finding a cascading
-- foreign key there is how this defect gets rebuilt. Its export sibling is
-- deliberately NOT changed: an export request has no reason to outlive the
-- account that asked for it, and widening the change to "every request table"
-- would be scope rather than remediation.
-- ---------------------------------------------------------------------------
ALTER TABLE audit.data_deletion_request
    DROP CONSTRAINT IF EXISTS data_deletion_request_user_id_fkey;

COMMENT ON COLUMN audit.data_deletion_request.user_id IS
  'Durable subject identifier. Not a foreign key: a deletion request must outlive the account it describes (PD-9). This table is currently unused — identity.account_lifecycle is the live ledger.';

-- ---------------------------------------------------------------------------
-- 4 · Reading the ledger once the account is gone
--
-- The RLS policies stay keyed on `app.user_id`, and that is correct rather than
-- an oversight: while the account exists its owner may read their own status,
-- and once it is gone no session can ever present that id again, so the row
-- becomes invisible to every ordinary role by construction. No policy change is
-- needed to achieve that and adding one would only create a second rule saying
-- the same thing.
--
-- The worker reaches these rows through the Entry 11B2 keyhole — three
-- SECURITY DEFINER functions with pinned search_path — which never joined
-- `identity.user_account` and therefore keeps working with no account row at
-- all. That is asserted in tests/security/test_pd9_durable_ledger.py rather
-- than assumed here.
--
-- One consequence worth stating: `identity.account_deletion_state(uuid)` is how
-- login, admission and the worker preflight ask whether an account is being
-- deleted. It reads the ledger by subject id and never touched user_account, so
-- it also continues to answer correctly for a subject whose account is gone.
-- ---------------------------------------------------------------------------
