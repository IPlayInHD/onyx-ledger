-- =============================================================================
-- Onyx Ledger — 68 · BillShield restricted runtime identity (grants only)
-- Alembic revision: 0075_billshield_runtime_identity
--
-- Slice 3A of docs/architecture/billshield-integration-plan.md: the enumerated
-- privileges of the `onyx_billshield_worker` group role that
-- 67_billshield_foundation.sql created EMPTY, plus the three schema grants that
-- make `ref.current_app_user()` and the deletion-cutoff function reachable.
--
-- THIS FILE CREATES NO OBJECT. No table, no column, no policy, no function, no
-- role, no index. It is a privilege migration and nothing else, which is what
-- makes it reviewable as one: every statement below is a GRANT, and the
-- downgrade is the same list as REVOKE.
--
-- EVERY DML GRANT IS COLUMN-SCOPED. There is no table-level SELECT, INSERT or
-- UPDATE on any of the four tables, and the security suite asserts that set is
-- EMPTY rather than merely small. The reason is not tidiness:
--
--   * a table-level verb authorizes every column the table has AND every column
--     a later migration adds, so the boundary widens with nobody writing a
--     grant or reviewing one;
--   * `reject_extraction_run_rewrite()` freezes exactly four columns and cannot
--     freeze a column that does not exist yet, so a trigger is defence in depth
--     and not the boundary; and
--   * "a column list has to be edited when a field is added" is the DESIRED
--     property. Making a new column writable should cost a reviewed migration.
--
-- WHY AN ALLOWLIST AND NOT "NO DIRECT TABLE PRIVILEGE". The freshness relay
-- holds frozenset() because its whole job is calling four functions. BillShield's
-- is not: its tenant unit of work exists precisely to read and write BillShield
-- rows, and a role with no privilege could not do that. So the model is
-- enumerated VERBS on enumerated COLUMNS of enumerated tables, each entry
-- traceable to one current use case, and everything else withheld (plan §5.4.6).
--
-- TWO COLUMNS BELOW ARE REQUIRED BY POSTGRESQL RATHER THAN BY WORKER CODE, and
-- both were established by measurement rather than assumption:
--
--   * `bill.user_id` — the parent-derived policies on `extraction_run` and both
--     candidate tables evaluate `b.user_id = ref.current_app_user()` in a
--     subquery that runs as the CALLER. Without SELECT on this column those
--     policies are refused outright, even though the worker never selects it.
--   * `extraction_run.bill_id` — both candidate policies join
--     `extraction_run r JOIN bill b ON b.id = r.bill_id`, again as the caller.
--
-- WHAT IS DELIBERATELY NOT HERE, and each omission is a decision:
--
--   * Any privilege on `billshield.job_outbox`. The claim/complete/fail
--     keyholes are the only outbox interface and they arrive in a later
--     sub-slice. Granting the worker a direct outbox verb now would make the
--     keyhole design optional before it exists.
--   * Any privilege on `billshield.provider` / `provider_category`. §7.2 is
--     that a provider is never resolved automatically from extracted issuer
--     text, so no use case at this sub-slice proves the read.
--   * Any read of an extraction VALUE. `extraction_run` SELECT is two columns —
--     identity and parent — and the candidate tables have no SELECT at all. The
--     API serves the review screens under its own grant.
--   * `DELETE` anywhere. BillShield's deletion model is a tombstone plus
--     privacy-worker erasure (§7.3, §7.4); the component that processes
--     untrusted files must not be able to destroy a tenant's rows.
--   * `TRUNCATE` and `REFERENCES` anywhere. A policy cannot filter a
--     whole-table wipe, and nothing outside this schema should be able to pin
--     its rows.
--   * Any sequence privilege. The schema has no sequence — every key is
--     `uuid DEFAULT ref.uuid_generate_v7()` — so a sequence grant here would
--     be a grant on nothing that a later serial column would silently inherit.
--   * Any privilege on any `identity` table. The schema grant below buys name
--     resolution; the definer function does the reading under its owner's
--     rights and returns one closed token.
--   * Any `EXECUTE` grant or `PUBLIC` revocation for `ref.current_app_user()`.
--     It is a plain STABLE function whose default PUBLIC execute right this
--     repository never revokes (`16_rls_grants.sql:37-40`); schema `USAGE` is
--     the whole of what the worker needs, and changing that function's grant
--     design would alter behaviour for every existing role.
--   * The `onyx_billshield` LOGIN itself. Roles are cluster-wide and their
--     passwords are not schema; the login is declared by the provisioning
--     surface (`infra/modules/database/bootstrap_runtime_logins.sql` for
--     production, `scripts/run_backend_tests.sh` for the local harness).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Schema USAGE — THREE SEPARATE STATEMENTS, naming three schemas.
--
-- `00_extensions_roles.sql:113-115` is a SINGLE statement granting USAGE on
-- fourteen schemas to `onyx_app_rw` and `onyx_app_ro`. Adding the worker to
-- that line would hand the component that parses untrusted bill files name
-- resolution across every Tax Assurance schema — the exact opposite of §5.4.6 —
-- so that line is never edited and these stand alone (decision 21.1(15)).
--
-- USAGE is name resolution, not table access. `billshield` is where the
-- worker's tables live; `ref` is what makes `ref.current_app_user()` — and with
-- it every RLS policy on those tables — resolvable; `identity` is what makes
-- the deletion-cutoff function nameable. None of the three carries a table.
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA billshield TO onyx_billshield_worker;
GRANT USAGE ON SCHEMA ref        TO onyx_billshield_worker;
GRANT USAGE ON SCHEMA identity   TO onyx_billshield_worker;

-- -----------------------------------------------------------------------------
-- The deletion cutoff (plan §5.4.7).
--
-- A task queued at T1 is picked up at T3; deletion was requested at T2 and
-- nothing in the queue can express that. The claim unit of work sets no
-- `app.user_id`, so a DIRECT read of `identity.account_lifecycle` would be
-- hidden by that table's policy, see nothing, and let every task through — a
-- check that appears to work and refuses nothing.
--
-- `identity.account_deletion_state(uuid)` is SQL STABLE SECURITY DEFINER with a
-- pinned `search_path`, returns a closed state code or NULL, and reads under
-- its owner's rights (`41_account_lifecycle.sql:580-594`). One EXECUTE grant,
-- and no privilege on any identity table.
-- -----------------------------------------------------------------------------
GRANT EXECUTE ON FUNCTION identity.account_deletion_state(uuid)
    TO onyx_billshield_worker;

-- -----------------------------------------------------------------------------
-- `billshield.bill` — an eleven-column read and a two-column write.
--
-- SELECT, one reason per column:
--   id              locate the bill; the WHERE clause of the UPDATE below
--   user_id         REQUIRED BY POSTGRESQL for the parent-derived policies on
--                   extraction_run and both candidate tables, whose subqueries
--                   run as the caller. The worker never selects it itself.
--   status          inspect lifecycle state before transitioning it
--   storage_key     obtain the artifact; the generated locator is the only
--                   address the bytes have
--   file_sha256     verify the artifact — this is the digest an extraction
--                   run's input_sha256 is bound to by the composite foreign key
--   byte_size       verify the artifact against the accepted size bounds
--   artifact_format verify the artifact: which parse path is legal
--   page_count      verify the artifact against the page ceiling
--   deleted_at      enforce deletion refusal: a tombstoned bill is not processed
--   erased_at       enforce deletion refusal: an erased bill has no bytes left
--   row_version     read-modify-write — `row_version = row_version + 1` READS
--                   the column, and is refused without SELECT on it
--
-- ABSENT: `created_at`, which is history the worker has no use for, and
-- `updated_at`, which the `ref.set_updated_at()` trigger owns — a runtime that
-- could read the trigger's stamp is one step from wanting to write it.
--
-- UPDATE is two columns: `status`, because the worker owns the §7.3 transitions
-- — uploaded→scanning, scanning→rejected|extracting, extracting→needs_review|
-- failed — and `row_version`, because those transitions are optimistically
-- concurrent. Everything else on the row belongs to somebody else: `deleted_at`
-- to the customer's request, `erased_at` to the privacy worker's claim that
-- physical erasure happened, the four artifact facts to upload completion, and
-- `id`/`user_id`/`storage_key` to immutable identity.
--
-- No INSERT: bills are created by the API at upload. No DELETE: the deletion
-- model is a tombstone plus privacy-worker erasure.
-- -----------------------------------------------------------------------------
GRANT SELECT (id, user_id, status, storage_key, file_sha256, byte_size,
              artifact_format, page_count, deleted_at, erased_at, row_version)
    ON billshield.bill TO onyx_billshield_worker;
GRANT UPDATE (status, row_version)
    ON billshield.bill TO onyx_billshield_worker;

-- -----------------------------------------------------------------------------
-- `billshield.extraction_run` — the worker's own attempt record.
--
-- INSERT expresses "CREATE A RUNNING ATTEMPT", which is exactly two columns:
-- the parent and the finalized digest the composite foreign key binds it to.
-- `status` defaults to 'running', `id` and `created_at` are server-owned, and
-- `completed_at` starts NULL — none of them is client-suppliable, so a worker
-- cannot insert a row that is already terminal, choose a run's identity, or
-- backdate when an extraction happened. Adapter identity is deliberately not
-- here: the schema permits a running row to acquire it later, so it is written
-- by the finalizing UPDATE and this grant stays at two columns.
--
-- SELECT is two columns and neither is an extraction value:
--   id       the INSERT's RETURNING, and the WHERE clause of the UPDATE below
--   bill_id  REQUIRED BY POSTGRESQL: both candidate policies join
--            `extraction_run r JOIN bill b ON b.id = r.bill_id` as the caller
--
-- UPDATE is the exact terminal-finalization set of the current extraction
-- contract — the outcome and its stamp, the two closed outcome vocabularies,
-- adapter provenance, success identity, and the nine bill-level candidate
-- triples. `id`, `bill_id`, `input_sha256` and `created_at` are ABSENT, so an
-- attempt to rewrite a run's identity is refused by the ACL BEFORE any trigger
-- runs. `reject_extraction_run_rewrite()` still freezes the same four and still
-- refuses any UPDATE to a row that is no longer `running`; it is defence in
-- depth, not the boundary, and it cannot cover a column added later.
--
-- No DELETE: an attempt, including a failed one, is the audit record of what
-- the extractor was asked and what it answered.
-- -----------------------------------------------------------------------------
GRANT SELECT (id, bill_id)
    ON billshield.extraction_run TO onyx_billshield_worker;
GRANT INSERT (bill_id, input_sha256)
    ON billshield.extraction_run TO onyx_billshield_worker;
GRANT UPDATE (
        status, completed_at,
        refusal_code, failure_code,
        adapter_code, model_version, prompt_version,
        extraction_schema_version, currency, response_hash,
        issuer_name_value, issuer_name_confidence, issuer_name_evidence,
        service_category_value, service_category_confidence,
        service_category_evidence,
        statement_date_value, statement_date_confidence,
        statement_date_evidence,
        billing_period_start, billing_period_end,
        billing_period_confidence, billing_period_evidence,
        amount_due_value, amount_due_confidence, amount_due_evidence,
        previous_balance_value, previous_balance_confidence,
        previous_balance_evidence,
        payments_applied_value, payments_applied_confidence,
        payments_applied_evidence,
        subtotal_before_tax_value, subtotal_before_tax_confidence,
        subtotal_before_tax_evidence,
        total_tax_value, total_tax_confidence, total_tax_evidence)
    ON billshield.extraction_run TO onyx_billshield_worker;

-- -----------------------------------------------------------------------------
-- `billshield.charge_candidate` and `billshield.promotion_candidate` —
-- a column-scoped INSERT and nothing else.
--
-- The grants name the extracted candidate fields. `id` and `created_at` are
-- excluded from both: a runtime that could supply `id` would choose a row's
-- primary key, and one that could supply `created_at` would backdate the record
-- of when the extraction produced it.
--
-- No UPDATE: extracted candidates are immutable facts about one attempt
-- (§21.1(11)). No SELECT: no use case at this sub-slice reads them back — the
-- API serves the review screens under its own grant. No DELETE: re-extraction
-- creates a new run, it does not rewrite an old one.
--
-- Both tables' policies are parent-derived through `extraction_run -> bill`,
-- which is why the two SELECT grants above are load-bearing for these INSERTs:
-- the policy subquery resolves as the worker, not as the table owner.
-- -----------------------------------------------------------------------------
GRANT INSERT (extraction_run_id, position,
              label_text, label_confidence, label_evidence,
              amount, amount_confidence, amount_evidence,
              kind, kind_confidence,
              cadence_value, cadence_confidence, cadence_evidence,
              service_period_start, service_period_end,
              service_period_confidence, service_period_evidence)
    ON billshield.charge_candidate TO onyx_billshield_worker;
GRANT INSERT (extraction_run_id, position, charge_position,
              expiry_date, expiry_confidence, expiry_evidence)
    ON billshield.promotion_candidate TO onyx_billshield_worker;

-- -----------------------------------------------------------------------------
-- Everyone else is unchanged by this migration.
--
-- `onyx_app_rw` keeps exactly the grants 67 gave it; `onyx_app_ro`,
-- `onyx_kb_admin`, `onyx_audit_writer` and PUBLIC keep nothing; and no policy,
-- trigger, constraint or default privilege is touched. A privilege migration
-- that also changed behaviour would not be reviewable as a privilege
-- migration.
-- -----------------------------------------------------------------------------
