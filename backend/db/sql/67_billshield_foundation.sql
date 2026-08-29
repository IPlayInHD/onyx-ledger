-- =============================================================================
-- Onyx Ledger — 67 · BillShield database foundation (schema: billshield)
-- Alembic revision: 0074_billshield_foundation
--
-- Slice 2 of docs/architecture/billshield-integration-plan.md: the seven-table
-- persistence foundation, its tenancy, and the exact API grants. It stores no
-- bill bytes, no raw provider output, and no filename, and it creates no
-- runtime that can process a bill — Slice 3 adds the worker login, its grants,
-- and the claim/complete/fail keyholes.
--
-- WHAT IS DELIBERATELY NOT HERE, and each omission is a decision:
--
--   * The `onyx_billshield` LOGIN. The group role below is created empty so
--     its privilege posture is assertable from the first migration; the login
--     that assumes it, and every grant it will hold, are Slice 3.
--   * Claim/complete/fail functions. The outbox carries every column those
--     keyholes need, so Slice 3 adds functions rather than redesigning a table.
--   * `last_error_code` on the outbox. A closed code needs a committed Python
--     authority (plan §7.1); the runtime failure vocabulary arrives with the
--     worker in Slice 3. An open-ended uppercase text column now would be a
--     "closed code" that is really free text.
--   * Any correction column on a candidate. Extracted candidates are immutable
--     facts; a user's correction becomes a structurally distinct confirmed
--     observation in a later slice, so an extraction can always be re-derived
--     and its response hash recomputed.
--   * Object erasure. `erased_at` is the truth column the privacy worker will
--     stamp in Slice 3; nothing here deletes an object.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS billshield;

COMMENT ON SCHEMA billshield IS
    'BillShield service. Global catalogue tables (provider, provider_category) '
    'coexist with tenant-derived operational tables; the tenant tables carry '
    'ENABLE + FORCE row-level security resolving through ref.current_app_user().';

-- PUBLIC gets nothing. A new schema grants PUBLIC no USAGE by default; the
-- revoke is written anyway, because "we relied on a default" is how a later
-- migration silently widens a boundary nobody restated.
REVOKE ALL ON SCHEMA billshield FROM PUBLIC;

-- -----------------------------------------------------------------------------
-- The restricted background role, created EMPTY.
--
-- It holds no schema USAGE, no table privilege, and no function EXECUTE in this
-- slice, and there is no login that can assume it. That is the point: a role
-- that exists with nothing is a role whose posture the privilege tests can
-- assert today, rather than a claim deferred until the slice that grants it.
-- Same shape as `onyx_freshness_worker` (29_ioe_outbox_and_projection.sql:228)
-- and `onyx_privacy_worker` (41_account_lifecycle.sql:403).
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_billshield_worker') THEN
        CREATE ROLE onyx_billshield_worker NOLOGIN;
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- Evidence locators — a governed JSONB domain, not an arbitrary blob.
--
-- Per-field evidence is a bounded list of page + normalized bounding box, and
-- it is the one place this schema stores a structure rather than a column. It
-- is governed the way `tax_kb.source_citation.locator` is: the shape is
-- constrained by the database, not by the writer's good intentions.
--
-- Coordinates are canonical DECIMAL STRINGS, never JSON numbers: a JSON number
-- is a float on the way in and out of most drivers, and the extraction contract
-- hashes these values. The regex admits exactly the unit interval at the
-- contract's scale — `0`, `0.xxxxxx` (1-6 places), `1`, `1.0` … `1.000000` —
-- so "1.5" and "0.1234567" are refused by the database, not merely by the
-- parser.
--
-- Evidence carries NO text snippet, and the key closure below is what makes
-- that structural: a locator with a `text` key is refused, so bill text cannot
-- enter the database through this column even if a future writer tried.
--
-- WHAT THIS DOMAIN DOES NOT ENFORCE, stated rather than implied: canonical
-- SORT ORDER and duplicate-freeness within a locator list. Both need a
-- per-element comparison a CHECK cannot express without a subquery. The
-- committed parser already sorts and de-duplicates before anything is
-- persisted, and the contract-parity test proves a persisted extraction
-- reproduces its `extraction_identity()` byte for byte — which fails if the
-- stored order ever diverges.
--
-- Nor does it enforce `page <= bill.page_count`: a domain sees one value, not
-- the row that will hold it or the bill that row belongs to. The parser bounds
-- the page against the artifact's real page count before anything reaches the
-- database, and this comment exists so nobody reads the positive-integer check
-- as the stronger claim.
-- -----------------------------------------------------------------------------
CREATE DOMAIN billshield.evidence_locators AS jsonb
    CONSTRAINT evidence_locators_is_bounded_array CHECK (
        jsonb_typeof(VALUE) = 'array'
        AND jsonb_array_length(VALUE) BETWEEN 1 AND 16
    )
    CONSTRAINT evidence_locators_are_objects CHECK (
        VALUE @@ '!exists($[*] ? (@.type() != "object"))'
    )
    CONSTRAINT evidence_locators_have_no_other_key CHECK (
        VALUE @@ '!exists($[*].keyvalue() ? (@.key != "page" && @.key != "x0"
                  && @.key != "y0" && @.key != "x1" && @.key != "y1"))'
    )
    CONSTRAINT evidence_locators_are_complete CHECK (
        VALUE @@ '!exists($[*] ? (!exists(@.page) || !exists(@.x0)
                  || !exists(@.y0) || !exists(@.x1) || !exists(@.y1)))'
    )
    CONSTRAINT evidence_locators_page_is_positive_integer CHECK (
        VALUE @@ '!exists($[*] ? (@.page.type() != "number" || @.page < 1
                  || @.page.floor() != @.page))'
    )
    CONSTRAINT evidence_locators_are_canonical_unit_decimals CHECK (
        VALUE @@ '!exists($[*] ? (@.x0.type() != "string"
                  || !(@.x0 like_regex "^(0(\\.[0-9]{1,6})?|1(\\.0{1,6})?)$")))'
        AND VALUE @@ '!exists($[*] ? (@.y0.type() != "string"
                  || !(@.y0 like_regex "^(0(\\.[0-9]{1,6})?|1(\\.0{1,6})?)$")))'
        AND VALUE @@ '!exists($[*] ? (@.x1.type() != "string"
                  || !(@.x1 like_regex "^(0(\\.[0-9]{1,6})?|1(\\.0{1,6})?)$")))'
        AND VALUE @@ '!exists($[*] ? (@.y1.type() != "string"
                  || !(@.y1 like_regex "^(0(\\.[0-9]{1,6})?|1(\\.0{1,6})?)$")))'
    )
    -- A box must have area. `.double()` is load-bearing: these are STRINGS, and
    -- a string comparison calls x0="0.1", x1="0.10" a valid box when it is in
    -- fact zero-width. Measured on PostgreSQL 16 before being relied on.
    CONSTRAINT evidence_locators_have_positive_area CHECK (
        VALUE @@ '!exists($[*] ? (@.x0.double() >= @.x1.double()
                  || @.y0.double() >= @.y1.double()))'
    );

COMMENT ON DOMAIN billshield.evidence_locators IS
    'Bounded list (1-16) of {page, x0, y0, x1, y1} locators. Page is a positive '
    'integer; coordinates are canonical unit-interval decimal STRINGS at up to '
    'six places. No other key is permitted, so no bill text can be stored here.';

-- =============================================================================
-- GLOBAL CATALOGUE
--
-- No user data, no RLS, runtime read-only, and no automatic resolution from an
-- extracted bill issuer. `bill_issuer_name` in the extraction contract is
-- UNTRUSTED text read off a document; a provider row is governed reference
-- data. Nothing in this slice joins the two, and no column exists that could.
-- =============================================================================

CREATE TABLE billshield.provider (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code        text NOT NULL,
    name        text NOT NULL,
    country     text NOT NULL DEFAULT 'CA',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_billshield_provider_code CHECK (code ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    CONSTRAINT ck_billshield_provider_name CHECK (length(btrim(name)) > 0),
    CONSTRAINT ck_billshield_provider_country CHECK (country = 'CA'),
    CONSTRAINT uq_billshield_provider_code UNIQUE (code)
);

COMMENT ON TABLE billshield.provider IS
    'Global commercial provider identity. NO category column: a Canadian '
    'provider commonly spans mobile, internet, television, home phone and '
    'bundles, and a single scalar would have to lie about all but one. '
    'Capabilities live in billshield.provider_category. Runtime read-only.';
COMMENT ON COLUMN billshield.provider.code IS
    'Operator-assigned stable identifier. Not derived from, and never matched '
    'against, an extracted bill_issuer_name.';

CREATE TABLE billshield.provider_category (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    provider_id uuid NOT NULL REFERENCES billshield.provider(id) ON DELETE CASCADE,
    category    text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_billshield_provider_category_value CHECK (category IN (
        'MOBILE', 'INTERNET', 'TV', 'HOME_PHONE', 'BUNDLE', 'STREAMING',
        'OTHER_SUBSCRIPTION')),
    CONSTRAINT uq_billshield_provider_category UNIQUE (provider_id, category)
);

COMMENT ON TABLE billshield.provider_category IS
    'Which governed service categories a provider offers. One row per '
    'capability, unique per (provider, category). The category vocabulary is '
    'the committed extraction contract ServiceCategory, mirrored exactly.';

CREATE INDEX ix_billshield_provider_category_provider
    ON billshield.provider_category (provider_id);

-- =============================================================================
-- TENANT-DERIVED OPERATIONAL TABLES
-- =============================================================================

-- -----------------------------------------------------------------------------
-- billshield.bill — the tenant root.
--
-- The row is metadata; the bytes live in the encrypted versioned bucket. The
-- object key is a GENERATED column, not a validated one: a key that merely
-- MATCHES `{uuid}/billshield/v1/{uuid}` proves nothing about whose row carries
-- it, so the database computes it from this row's own ids and no writer can
-- supply, forge, or move it.
-- -----------------------------------------------------------------------------
CREATE TABLE billshield.bill (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id         uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    status          text NOT NULL DEFAULT 'upload_pending',
    storage_key     text NOT NULL GENERATED ALWAYS AS
                        (user_id::text || '/billshield/v1/' || id::text) STORED,
    -- Finalized artifact facts: all four arrive together at upload completion
    -- and none of them may change afterwards (trigger below).
    file_sha256     text,
    byte_size       bigint,
    artifact_format text,
    page_count      integer,
    -- Truthful deletion timestamps. `deleted_at` is the LOGICAL deletion time,
    -- set when the customer's request is accepted and the artifact stops being
    -- served. `erased_at` is stamped only after the privacy worker has proven
    -- physical erasure (Slice 3). A schema that used one column for both would
    -- make "deleted" mean either "we stopped showing it" or "the bytes are
    -- gone" depending on who was reading.
    deleted_at      timestamptz,
    erased_at       timestamptz,
    row_version     integer NOT NULL DEFAULT 1,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_billshield_bill_status CHECK (status IN (
        'upload_pending', 'uploaded', 'scanning', 'rejected', 'extracting',
        'needs_review', 'failed', 'confirmed', 'deletion_pending', 'deleted')),
    CONSTRAINT ck_billshield_bill_file_sha256 CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_billshield_bill_byte_size CHECK (byte_size > 0),
    CONSTRAINT ck_billshield_bill_artifact_format CHECK (artifact_format IN (
        'pdf_native', 'pdf_scanned', 'image_png', 'image_jpeg')),
    CONSTRAINT ck_billshield_bill_page_count CHECK (page_count >= 1),
    CONSTRAINT ck_billshield_bill_row_version CHECK (row_version >= 1),
    -- The four artifact facts are one decision, taken once.
    CONSTRAINT ck_billshield_bill_artifact_is_whole CHECK (
        num_nonnulls(file_sha256, byte_size, artifact_format, page_count) IN (0, 4)),
    -- Anything past upload has a finalized artifact; a bill deleted before it
    -- was ever completed legitimately has none. `deletion_pending`/`deleted`
    -- are permitted without one for exactly that case — deletion that began
    -- before upload completion — not as a general exemption.
    CONSTRAINT ck_billshield_bill_processing_has_artifact CHECK (
        file_sha256 IS NOT NULL
        OR status IN ('upload_pending', 'deletion_pending', 'deleted')),
    -- And the other direction: an upload that has not completed cannot already
    -- know its own digest. Without this, a row could claim finalized artifact
    -- facts while still advertising itself as awaiting the upload.
    CONSTRAINT ck_billshield_bill_pending_is_not_finalized CHECK (
        status <> 'upload_pending' OR file_sha256 IS NULL),
    CONSTRAINT ck_billshield_bill_deletion_timestamps CHECK (
        CASE status
            WHEN 'deletion_pending' THEN deleted_at IS NOT NULL AND erased_at IS NULL
            WHEN 'deleted'          THEN deleted_at IS NOT NULL AND erased_at IS NOT NULL
            ELSE deleted_at IS NULL AND erased_at IS NULL
        END),
    CONSTRAINT ck_billshield_bill_erasure_follows_deletion CHECK (
        erased_at IS NULL OR erased_at >= deleted_at),
    -- Referenced compositely so a child can never name a row belonging to
    -- another tenant, or a digest belonging to another bill.
    CONSTRAINT uq_billshield_bill_identity_owner UNIQUE (id, user_id),
    CONSTRAINT uq_billshield_bill_identity_digest UNIQUE (id, file_sha256)
);

COMMENT ON TABLE billshield.bill IS
    'One uploaded customer bill and its lifecycle. Metadata only: no bytes, no '
    'customer filename, no provider text. Deletion is a tombstone plus '
    'privacy-worker erasure, never a row delete by a runtime.';
COMMENT ON COLUMN billshield.bill.storage_key IS
    'GENERATED from this row: user_id/billshield/v1/id. Shape is not ownership, '
    'so the value is computed rather than validated — a key naming another '
    'tenant cannot be written because no writer supplies this column at all.';
COMMENT ON COLUMN billshield.bill.deleted_at IS
    'LOGICAL deletion: the customer asked, and the artifact stops being served '
    'immediately. Not proof that any byte is gone.';
COMMENT ON COLUMN billshield.bill.erased_at IS
    'PHYSICAL erasure completion, stamped by the privacy worker only after every '
    'object version and delete marker is proven gone (Slice 3).';

CREATE INDEX ix_billshield_bill_user ON billshield.bill (user_id);
CREATE INDEX ix_billshield_bill_deletion_pending
    ON billshield.bill (created_at) WHERE status = 'deletion_pending';

-- -----------------------------------------------------------------------------
-- billshield.extraction_run — one immutable extraction attempt.
--
-- The composite foreign key is doing two jobs at once: it is the tenancy edge
-- (the run belongs to that bill, and the bill belongs to a tenant) and it is
-- the artifact binding — `input_sha256` must equal the FINALIZED digest of the
-- very bill named, so a run can never be repointed at a different artifact and
-- can never describe bytes nobody finalized.
-- -----------------------------------------------------------------------------
CREATE TABLE billshield.extraction_run (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    bill_id         uuid NOT NULL,
    input_sha256    text NOT NULL,
    status          text NOT NULL DEFAULT 'running',
    -- Two different closed vocabularies for two different outcomes, each with a
    -- committed Python authority:
    --   refusal_code  — the extractor READ the document and declined it whole
    --                   (extraction.codes.RefusalCode);
    --   failure_code  — the provider's output did not survive the strict parse
    --                   (extraction.codes.ExtractionParseCode).
    -- Neither is the future OUTBOX/runtime failure vocabulary, which has no
    -- authority yet and therefore no column anywhere in this schema.
    refusal_code    text,
    failure_code    text,
    -- Trusted adapter provenance, declared by adapter code. Never confused with
    -- the untrusted bill issuer read off the document, which is a candidate
    -- below like any other extracted value.
    adapter_code    text,
    model_version   text,
    prompt_version  text,
    extraction_schema_version text,
    currency        text,
    response_hash   text,
    completed_at    timestamptz,

    -- Bill-level candidates. Each is a value + confidence + evidence triple,
    -- all three NULL together. All-NULL means the extractor produced NO
    -- candidate — it is NEVER a statement that the document lacks the field.
    issuer_name_value               text,
    issuer_name_confidence          ref.rate,
    issuer_name_evidence            billshield.evidence_locators,
    service_category_value          text,
    service_category_confidence     ref.rate,
    service_category_evidence       billshield.evidence_locators,
    statement_date_value            date,
    statement_date_confidence       ref.rate,
    statement_date_evidence         billshield.evidence_locators,
    billing_period_start            date,
    billing_period_end              date,
    billing_period_confidence       ref.rate,
    billing_period_evidence         billshield.evidence_locators,
    amount_due_value                ref.money_amt,
    amount_due_confidence           ref.rate,
    amount_due_evidence             billshield.evidence_locators,
    previous_balance_value          ref.money_amt,
    previous_balance_confidence     ref.rate,
    previous_balance_evidence       billshield.evidence_locators,
    payments_applied_value          ref.money_amt,
    payments_applied_confidence     ref.rate,
    payments_applied_evidence       billshield.evidence_locators,
    subtotal_before_tax_value       ref.money_amt,
    subtotal_before_tax_confidence  ref.rate,
    subtotal_before_tax_evidence    billshield.evidence_locators,
    total_tax_value                 ref.money_amt,
    total_tax_confidence            ref.rate,
    total_tax_evidence              billshield.evidence_locators,
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fk_billshield_extraction_run_bill_artifact
        FOREIGN KEY (bill_id, input_sha256)
        REFERENCES billshield.bill (id, file_sha256) ON DELETE CASCADE,
    CONSTRAINT ck_billshield_extraction_run_status CHECK (status IN (
        'running', 'succeeded', 'refused', 'failed')),
    CONSTRAINT ck_billshield_extraction_run_input_sha256
        CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_billshield_extraction_run_response_hash
        CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    -- Exactly the committed RefusalCode vocabulary, and only on a refusal.
    CONSTRAINT ck_billshield_extraction_run_refusal_code CHECK (
        refusal_code IN ('UNREADABLE', 'UNSUPPORTED_FORMAT',
                         'UNSUPPORTED_SERVICE_SCOPE', 'NO_USABLE_EXTRACTION')),
    CONSTRAINT ck_billshield_extraction_run_refusal_coherent CHECK (
        (status = 'refused') = (refusal_code IS NOT NULL)),
    -- Exactly the committed ExtractionParseCode vocabulary, and only on a
    -- failure. Reused rather than restated: the parser owns these eighteen
    -- codes, and the parity test compares the two sets in both directions.
    CONSTRAINT ck_billshield_extraction_run_failure_code CHECK (
        failure_code IN (
            'UNSUPPORTED_SCHEMA_VERSION', 'UNKNOWN_FIELD', 'MISSING_REQUIRED_FIELD',
            'UNKNOWN_FIELD_STATE', 'MALFORMED_VALUE', 'INEXACT_NUMBER',
            'WRONG_SCALE', 'OUT_OF_RANGE', 'UNSUPPORTED_CURRENCY',
            'UNKNOWN_ENUM_VALUE', 'INCOHERENT_DATES', 'INCOHERENT_SIGN',
            'INCOHERENT_CADENCE', 'INVALID_CHARGE_REFERENCE', 'EMPTY_SUCCESS',
            'EVIDENCE_MISSING', 'EVIDENCE_MALFORMED', 'EVIDENCE_OUT_OF_BOUNDS')),
    CONSTRAINT ck_billshield_extraction_run_failure_coherent CHECK (
        (status = 'failed') = (failure_code IS NOT NULL)),
    -- A success carries ALL THREE success fields; every other status carries
    -- NONE of them. An equality on the COUNT alone let a failed row keep a
    -- response hash, which reads as an auditable extraction that never was.
    CONSTRAINT ck_billshield_extraction_run_success_coherent CHECK (
        CASE WHEN status = 'succeeded'
             THEN num_nonnulls(response_hash, currency,
                               extraction_schema_version) = 3
             ELSE num_nonnulls(response_hash, currency,
                               extraction_schema_version) = 0
        END),
    CONSTRAINT ck_billshield_extraction_run_currency CHECK (currency = 'CAD'),
    -- Adapter identity is a PAIR. Half of it names a model nobody can attribute
    -- or an adapter whose version is unknown, and both are worse than nothing.
    CONSTRAINT ck_billshield_extraction_run_adapter_pair CHECK (
        num_nonnulls(adapter_code, model_version) <> 1),
    -- Every terminal run carries it; a running row may acquire it when the
    -- adapter is chosen, but never one half of it.
    CONSTRAINT ck_billshield_extraction_run_terminal_identity CHECK (
        status = 'running' OR num_nonnulls(adapter_code, model_version) = 2),
    -- An optional third component of the same identity, meaningless alone.
    CONSTRAINT ck_billshield_extraction_run_prompt_needs_adapter CHECK (
        prompt_version IS NULL OR adapter_code IS NOT NULL),
    CONSTRAINT ck_billshield_extraction_run_terminal_is_stamped CHECK (
        (status = 'running') = (completed_at IS NULL)),
    -- Candidate triples: value + confidence + evidence, together or absent.
    CONSTRAINT ck_billshield_run_issuer_name_triple CHECK (
        num_nonnulls(issuer_name_value, issuer_name_confidence, issuer_name_evidence) IN (0, 3)),
    CONSTRAINT ck_billshield_run_service_category_triple CHECK (
        num_nonnulls(service_category_value, service_category_confidence,
                     service_category_evidence) IN (0, 3)),
    CONSTRAINT ck_billshield_run_statement_date_triple CHECK (
        num_nonnulls(statement_date_value, statement_date_confidence,
                     statement_date_evidence) IN (0, 3)),
    CONSTRAINT ck_billshield_run_billing_period_quad CHECK (
        num_nonnulls(billing_period_start, billing_period_end,
                     billing_period_confidence, billing_period_evidence) IN (0, 4)),
    CONSTRAINT ck_billshield_run_billing_period_ordered CHECK (
        billing_period_start IS NULL OR billing_period_start <= billing_period_end),
    CONSTRAINT ck_billshield_run_amount_due_triple CHECK (
        num_nonnulls(amount_due_value, amount_due_confidence, amount_due_evidence) IN (0, 3)),
    CONSTRAINT ck_billshield_run_previous_balance_triple CHECK (
        num_nonnulls(previous_balance_value, previous_balance_confidence,
                     previous_balance_evidence) IN (0, 3)),
    CONSTRAINT ck_billshield_run_payments_applied_triple CHECK (
        num_nonnulls(payments_applied_value, payments_applied_confidence,
                     payments_applied_evidence) IN (0, 3)),
    CONSTRAINT ck_billshield_run_subtotal_before_tax_triple CHECK (
        num_nonnulls(subtotal_before_tax_value, subtotal_before_tax_confidence,
                     subtotal_before_tax_evidence) IN (0, 3)),
    CONSTRAINT ck_billshield_run_total_tax_triple CHECK (
        num_nonnulls(total_tax_value, total_tax_confidence, total_tax_evidence) IN (0, 3)),
    -- The parser's normalized sign rule for a payment.
    CONSTRAINT ck_billshield_run_payments_applied_sign CHECK (
        payments_applied_value IS NULL OR payments_applied_value <= 0),
    -- Confidences are unit-interval.
    CONSTRAINT ck_billshield_run_confidence_bounds CHECK (
        (issuer_name_confidence IS NULL OR issuer_name_confidence BETWEEN 0 AND 1)
        AND (service_category_confidence IS NULL OR service_category_confidence BETWEEN 0 AND 1)
        AND (statement_date_confidence IS NULL OR statement_date_confidence BETWEEN 0 AND 1)
        AND (billing_period_confidence IS NULL OR billing_period_confidence BETWEEN 0 AND 1)
        AND (amount_due_confidence IS NULL OR amount_due_confidence BETWEEN 0 AND 1)
        AND (previous_balance_confidence IS NULL OR previous_balance_confidence BETWEEN 0 AND 1)
        AND (payments_applied_confidence IS NULL OR payments_applied_confidence BETWEEN 0 AND 1)
        AND (subtotal_before_tax_confidence IS NULL OR subtotal_before_tax_confidence BETWEEN 0 AND 1)
        AND (total_tax_confidence IS NULL OR total_tax_confidence BETWEEN 0 AND 1)),
    CONSTRAINT ck_billshield_run_service_category_value CHECK (
        service_category_value IN ('MOBILE', 'INTERNET', 'TV', 'HOME_PHONE',
                                   'BUNDLE', 'STREAMING', 'OTHER_SUBSCRIPTION')),
    CONSTRAINT ck_billshield_run_issuer_name_length CHECK (
        length(issuer_name_value) BETWEEN 1 AND 200)
);

COMMENT ON TABLE billshield.extraction_run IS
    'One extraction attempt over one finalized bill artifact. Immutable once '
    'terminal. Stores normalized candidates, provenance and the response hash — '
    'never the raw provider payload, and never provider exception text.';
COMMENT ON COLUMN billshield.extraction_run.input_sha256 IS
    'The artifact digest actually read. Bound by composite FK to the SAME '
    'bill row''s finalized file_sha256, so a run cannot be repointed.';
COMMENT ON COLUMN billshield.extraction_run.response_hash IS
    'extraction_identity() over the accepted candidates — the plan §9.5 '
    'response hash, which is what is stored INSTEAD of the provider response.';
COMMENT ON COLUMN billshield.extraction_run.issuer_name_value IS
    'UNTRUSTED issuer text read off the document. Never resolved to a '
    'billshield.provider row; that deterministic resolver is a later slice.';

CREATE INDEX ix_billshield_extraction_run_bill ON billshield.extraction_run (bill_id);
-- One live extraction per bill (plan §8.3).
CREATE UNIQUE INDEX uq_billshield_extraction_run_live
    ON billshield.extraction_run (bill_id) WHERE status = 'running';

-- -----------------------------------------------------------------------------
-- billshield.charge_candidate — unconfirmed, immutable, ordered.
--
-- `position` is the charge's place in DOCUMENT order, which the extraction
-- contract treats as semantic and folds into its hash. Storing a set and
-- sorting later would silently change the identity of an extraction.
-- -----------------------------------------------------------------------------
CREATE TABLE billshield.charge_candidate (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    extraction_run_id   uuid NOT NULL
                        REFERENCES billshield.extraction_run(id) ON DELETE CASCADE,
    position            integer NOT NULL,
    label_text          text NOT NULL,
    label_confidence    ref.rate NOT NULL,
    label_evidence      billshield.evidence_locators NOT NULL,
    amount              ref.money_amt NOT NULL,
    amount_confidence   ref.rate NOT NULL,
    amount_evidence     billshield.evidence_locators NOT NULL,
    kind                text NOT NULL,
    kind_confidence     ref.rate NOT NULL,
    cadence_value       text,
    cadence_confidence  ref.rate,
    cadence_evidence    billshield.evidence_locators,
    service_period_start        date,
    service_period_end          date,
    service_period_confidence   ref.rate,
    service_period_evidence     billshield.evidence_locators,
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_billshield_charge_position CHECK (position >= 0),
    CONSTRAINT ck_billshield_charge_label_length CHECK (
        length(label_text) BETWEEN 1 AND 500),
    CONSTRAINT ck_billshield_charge_kind CHECK (kind IN (
        'RECURRING_FIXED', 'USAGE', 'ONE_TIME', 'TAX', 'CREDIT', 'FEE',
        'DEVICE_FINANCING', 'PRORATION', 'UNCLASSIFIED')),
    CONSTRAINT ck_billshield_charge_cadence_value CHECK (cadence_value IN (
        'MONTHLY', 'WEEKLY', 'BIWEEKLY', 'SEMI_MONTHLY', 'QUARTERLY',
        'SEMI_ANNUAL', 'ANNUAL', 'OTHER')),
    -- The parser's normalized sign rule, mirrored: a credit reduces the balance
    -- owed, the ordinary kinds increase it, and tax/proration/unclassified may
    -- go either way.
    CONSTRAINT ck_billshield_charge_sign CHECK (
        CASE kind
            WHEN 'CREDIT' THEN amount < 0
            WHEN 'TAX' THEN true
            WHEN 'PRORATION' THEN true
            WHEN 'UNCLASSIFIED' THEN true
            ELSE amount >= 0
        END),
    -- Cadence is only meaningful on a kind that recurs.
    CONSTRAINT ck_billshield_charge_cadence_kind CHECK (
        cadence_value IS NULL
        OR kind IN ('RECURRING_FIXED', 'DEVICE_FINANCING', 'UNCLASSIFIED')),
    CONSTRAINT ck_billshield_charge_cadence_triple CHECK (
        num_nonnulls(cadence_value, cadence_confidence, cadence_evidence) IN (0, 3)),
    CONSTRAINT ck_billshield_charge_service_period_quad CHECK (
        num_nonnulls(service_period_start, service_period_end,
                     service_period_confidence, service_period_evidence) IN (0, 4)),
    CONSTRAINT ck_billshield_charge_service_period_ordered CHECK (
        service_period_start IS NULL OR service_period_start <= service_period_end),
    CONSTRAINT ck_billshield_charge_confidence_bounds CHECK (
        label_confidence BETWEEN 0 AND 1
        AND amount_confidence BETWEEN 0 AND 1
        AND kind_confidence BETWEEN 0 AND 1
        AND (cadence_confidence IS NULL OR cadence_confidence BETWEEN 0 AND 1)
        AND (service_period_confidence IS NULL OR service_period_confidence BETWEEN 0 AND 1)),
    CONSTRAINT uq_billshield_charge_candidate_position UNIQUE (extraction_run_id, position)
);

COMMENT ON TABLE billshield.charge_candidate IS
    'An unconfirmed extracted charge. IMMUTABLE: a user correction is not an '
    'edit here, it becomes a structurally distinct confirmed observation in a '
    'later slice, so the extraction stays reproducible and re-hashable.';

CREATE INDEX ix_billshield_charge_candidate_run
    ON billshield.charge_candidate (extraction_run_id);

-- -----------------------------------------------------------------------------
-- billshield.promotion_candidate — an EXPLICITLY PRINTED expiry.
--
-- The contract binds a promotion to a charge by INDEX within the same
-- extraction. The composite foreign key preserves that association exactly
-- while making a reference into another run's charges impossible.
-- -----------------------------------------------------------------------------
CREATE TABLE billshield.promotion_candidate (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    extraction_run_id   uuid NOT NULL
                        REFERENCES billshield.extraction_run(id) ON DELETE CASCADE,
    position            integer NOT NULL,
    charge_position     integer,
    expiry_date         date NOT NULL,
    expiry_confidence   ref.rate NOT NULL,
    expiry_evidence     billshield.evidence_locators NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_billshield_promotion_position CHECK (position >= 0),
    CONSTRAINT ck_billshield_promotion_charge_position CHECK (charge_position >= 0),
    CONSTRAINT ck_billshield_promotion_confidence_bounds CHECK (
        expiry_confidence BETWEEN 0 AND 1),
    CONSTRAINT uq_billshield_promotion_candidate_position
        UNIQUE (extraction_run_id, position),
    CONSTRAINT fk_billshield_promotion_charge
        FOREIGN KEY (extraction_run_id, charge_position)
        REFERENCES billshield.charge_candidate (extraction_run_id, position)
        ON DELETE CASCADE
);

COMMENT ON TABLE billshield.promotion_candidate IS
    'An expiry date PRINTED on the bill, never inferred. A NULL charge_position '
    'means document or service level; a non-NULL one names a charge in the SAME '
    'extraction run, enforced compositely.';

CREATE INDEX ix_billshield_promotion_candidate_run
    ON billshield.promotion_candidate (extraction_run_id);

-- -----------------------------------------------------------------------------
-- billshield.job_outbox — transactional job intent.
--
-- Identifiers and one closed task code. There is no column here that could
-- hold a charge, an amount, a filename, or a provider's exception text, which
-- is the only durable way to keep bill content out of a queue.
--
-- The composite ownership edge is the load-bearing constraint: a policy that
-- checked only `user_id` would happily accept a row naming tenant A beside
-- tenant B's bill.
-- -----------------------------------------------------------------------------
CREATE TABLE billshield.job_outbox (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id     uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    bill_id     uuid NOT NULL,
    task_code   text NOT NULL,
    dedupe_key  uuid NOT NULL,
    claim_state text NOT NULL DEFAULT 'pending',
    claimed_by  text,
    claim_token uuid,
    claimed_at  timestamptz,
    processed_at timestamptz,
    attempts    smallint NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fk_billshield_job_outbox_bill_owner
        FOREIGN KEY (bill_id, user_id)
        REFERENCES billshield.bill (id, user_id) ON DELETE CASCADE,
    CONSTRAINT ck_billshield_job_outbox_task_code CHECK (task_code IN ('EXTRACT_BILL')),
    CONSTRAINT ck_billshield_job_outbox_claim_state CHECK (claim_state IN (
        'pending', 'claimed', 'completed', 'failed')),
    -- Worker identity is a bounded opaque token, not prose.
    CONSTRAINT ck_billshield_job_outbox_claimed_by CHECK (
        claimed_by ~ '^[a-z0-9][a-z0-9._-]{2,63}$'),
    CONSTRAINT ck_billshield_job_outbox_claim_is_coherent CHECK (
        (claim_state = 'claimed') =
        (num_nonnulls(claimed_by, claim_token, claimed_at) = 3)),
    CONSTRAINT ck_billshield_job_outbox_terminal_is_stamped CHECK (
        (claim_state IN ('completed', 'failed')) = (processed_at IS NOT NULL)),
    CONSTRAINT ck_billshield_job_outbox_attempts CHECK (attempts BETWEEN 0 AND 5),
    -- Two independent uniqueness facts, and neither implies the other.
    --
    -- The key is globally unique, so one idempotency handle names one intent.
    -- But the CALLER picks the key: a caller that generates a fresh UUID would
    -- enqueue the same extraction twice and the key constraint would not
    -- notice. The intent constraint is what actually says "one EXTRACT_BILL per
    -- bill" — retries are transitions on that row, not new rows.
    CONSTRAINT uq_billshield_job_outbox_dedupe UNIQUE (dedupe_key),
    CONSTRAINT uq_billshield_job_outbox_intent UNIQUE (task_code, bill_id)
);

COMMENT ON TABLE billshield.job_outbox IS
    'Transactional BillShield job intent. Identifiers and one closed task code '
    'only. The API enqueues through a COLUMN-SCOPED insert grant; claim, '
    'complete and fail are Slice 3 keyholes, and no runtime updates this table.';
COMMENT ON COLUMN billshield.job_outbox.dedupe_key IS
    'Opaque idempotency handle chosen by the caller, unique table-wide so one '
    'handle names one intent. It does NOT by itself prove one intent per task '
    'per bill — a caller with a fresh UUID would enqueue the same work twice; '
    'uq_billshield_job_outbox_intent is the constraint that says that.';
COMMENT ON COLUMN billshield.job_outbox.claim_state IS
    'Server-defaulted to pending. The API''s enqueue grant does not include '
    'this column, so a request path cannot create work that is already claimed.';

CREATE INDEX ix_billshield_job_outbox_user ON billshield.job_outbox (user_id);
CREATE INDEX ix_billshield_job_outbox_bill ON billshield.job_outbox (bill_id);
-- Deterministic oldest-first claim order with a unique tiebreak (plan §8.3).
CREATE INDEX ix_billshield_job_outbox_pending
    ON billshield.job_outbox (created_at, id) WHERE claim_state = 'pending';
CREATE INDEX ix_billshield_job_outbox_claimed
    ON billshield.job_outbox (claimed_at) WHERE claim_state = 'claimed';

-- =============================================================================
-- IMMUTABILITY
--
-- The grants below already withhold most of this. These triggers refuse it from
-- ANY role, which is the difference between "the application cannot rewrite
-- identity" and "identity cannot be rewritten".
-- =============================================================================

CREATE OR REPLACE FUNCTION billshield.reject_bill_identity_change()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.user_id IS DISTINCT FROM OLD.user_id THEN
        RAISE EXCEPTION 'billshield.bill identity is immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    -- Finalized artifact facts move from unset to set exactly once. Clearing
    -- them would let a bill forget which bytes it described; changing them
    -- would repoint it at somebody else's.
    IF OLD.file_sha256 IS NOT NULL AND NEW.file_sha256 IS DISTINCT FROM OLD.file_sha256 THEN
        RAISE EXCEPTION 'billshield.bill artifact digest is immutable once finalized'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.byte_size IS NOT NULL AND NEW.byte_size IS DISTINCT FROM OLD.byte_size THEN
        RAISE EXCEPTION 'billshield.bill artifact size is immutable once finalized'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.artifact_format IS NOT NULL
       AND NEW.artifact_format IS DISTINCT FROM OLD.artifact_format THEN
        RAISE EXCEPTION 'billshield.bill artifact format is immutable once finalized'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.page_count IS NOT NULL AND NEW.page_count IS DISTINCT FROM OLD.page_count THEN
        RAISE EXCEPTION 'billshield.bill page count is immutable once finalized'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'billshield.bill created_at is immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    -- Deletion facts are recorded once. Moving a tombstone's timestamp would
    -- rewrite when a customer asked to be forgotten; CLEARING it is worse — it
    -- resurrects a bill the customer has already been told is gone, and the
    -- status CHECK alone permits that because it only reads the pair as it
    -- stands after the statement.
    IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
        RAISE EXCEPTION 'billshield.bill deleted_at is immutable once recorded'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.erased_at IS NOT NULL AND NEW.erased_at IS DISTINCT FROM OLD.erased_at THEN
        RAISE EXCEPTION 'billshield.bill erased_at is immutable once recorded'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

ALTER FUNCTION billshield.reject_bill_identity_change() OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION billshield.reject_bill_identity_change() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_billshield_bill_identity ON billshield.bill;
CREATE TRIGGER trg_billshield_bill_identity
    BEFORE UPDATE ON billshield.bill
    FOR EACH ROW EXECUTE FUNCTION billshield.reject_bill_identity_change();

CREATE OR REPLACE FUNCTION billshield.reject_extraction_run_rewrite()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    -- A run is finalized exactly once: `running` may become terminal, and a
    -- terminal run is frozen. Anything else would let a recorded extraction be
    -- re-described after somebody read it.
    IF OLD.status <> 'running' THEN
        RAISE EXCEPTION 'billshield.extraction_run is immutable once terminal'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.bill_id IS DISTINCT FROM OLD.bill_id
       OR NEW.input_sha256 IS DISTINCT FROM OLD.input_sha256
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'billshield.extraction_run identity and input are immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

ALTER FUNCTION billshield.reject_extraction_run_rewrite() OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION billshield.reject_extraction_run_rewrite() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_billshield_extraction_run_immutable ON billshield.extraction_run;
CREATE TRIGGER trg_billshield_extraction_run_immutable
    BEFORE UPDATE ON billshield.extraction_run
    FOR EACH ROW EXECUTE FUNCTION billshield.reject_extraction_run_rewrite();

CREATE OR REPLACE FUNCTION billshield.reject_candidate_mutation()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION
        'billshield candidate rows are immutable extracted facts; % is not permitted',
        TG_OP
        USING ERRCODE = 'check_violation';
END;
$$;

ALTER FUNCTION billshield.reject_candidate_mutation() OWNER TO onyx_migrator;
REVOKE ALL ON FUNCTION billshield.reject_candidate_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_billshield_charge_candidate_immutable ON billshield.charge_candidate;
CREATE TRIGGER trg_billshield_charge_candidate_immutable
    BEFORE UPDATE ON billshield.charge_candidate
    FOR EACH ROW EXECUTE FUNCTION billshield.reject_candidate_mutation();

DROP TRIGGER IF EXISTS trg_billshield_promotion_candidate_immutable
    ON billshield.promotion_candidate;
CREATE TRIGGER trg_billshield_promotion_candidate_immutable
    BEFORE UPDATE ON billshield.promotion_candidate
    FOR EACH ROW EXECUTE FUNCTION billshield.reject_candidate_mutation();

-- Keep `updated_at` honest on the two tables that carry it.
DROP TRIGGER IF EXISTS trg_billshield_bill_updated_at ON billshield.bill;
CREATE TRIGGER trg_billshield_bill_updated_at
    BEFORE UPDATE ON billshield.bill
    FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();

DROP TRIGGER IF EXISTS trg_billshield_provider_updated_at ON billshield.provider;
CREATE TRIGGER trg_billshield_provider_updated_at
    BEFORE UPDATE ON billshield.provider
    FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();

-- =============================================================================
-- ROW-LEVEL SECURITY
--
-- ENABLE is what confines the ordinary roles; FORCE additionally subjects the
-- table OWNER (onyx_migrator) to the same policies. Every policy is FOR ALL
-- with matching USING and WITH CHECK: a USING-only policy reads correctly and
-- leaves ownership forgery wide open.
--
-- Children resolve ownership by walking to the bill EXPLICITLY rather than
-- carrying a denormalized user_id, because a denormalized column is a way to
-- CHANGE ownership (43_pd1_tenant_rls.sql:41-45).
--
-- The two global catalogue tables carry no RLS by design and are justified in
-- app/privacy/classification.py NON_RLS; least privilege for them is the grant.
-- =============================================================================

ALTER TABLE billshield.bill ENABLE ROW LEVEL SECURITY;
ALTER TABLE billshield.bill FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_bill ON billshield.bill
    FOR ALL
    USING (user_id = ref.current_app_user())
    WITH CHECK (user_id = ref.current_app_user());

ALTER TABLE billshield.extraction_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE billshield.extraction_run FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_extraction_run ON billshield.extraction_run
    FOR ALL
    USING (EXISTS (SELECT 1 FROM billshield.bill b
                   WHERE b.id = extraction_run.bill_id
                     AND b.user_id = ref.current_app_user()))
    WITH CHECK (EXISTS (SELECT 1 FROM billshield.bill b
                        WHERE b.id = extraction_run.bill_id
                          AND b.user_id = ref.current_app_user()));

ALTER TABLE billshield.charge_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE billshield.charge_candidate FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_charge_candidate ON billshield.charge_candidate
    FOR ALL
    USING (EXISTS (SELECT 1 FROM billshield.extraction_run r
                   JOIN billshield.bill b ON b.id = r.bill_id
                   WHERE r.id = charge_candidate.extraction_run_id
                     AND b.user_id = ref.current_app_user()))
    WITH CHECK (EXISTS (SELECT 1 FROM billshield.extraction_run r
                        JOIN billshield.bill b ON b.id = r.bill_id
                        WHERE r.id = charge_candidate.extraction_run_id
                          AND b.user_id = ref.current_app_user()));

ALTER TABLE billshield.promotion_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE billshield.promotion_candidate FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_promotion_candidate ON billshield.promotion_candidate
    FOR ALL
    USING (EXISTS (SELECT 1 FROM billshield.extraction_run r
                   JOIN billshield.bill b ON b.id = r.bill_id
                   WHERE r.id = promotion_candidate.extraction_run_id
                     AND b.user_id = ref.current_app_user()))
    WITH CHECK (EXISTS (SELECT 1 FROM billshield.extraction_run r
                        JOIN billshield.bill b ON b.id = r.bill_id
                        WHERE r.id = promotion_candidate.extraction_run_id
                          AND b.user_id = ref.current_app_user()));

ALTER TABLE billshield.job_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE billshield.job_outbox FORCE ROW LEVEL SECURITY;
-- Direct ownership, and no `user_id IS NULL` branch: unlike the freshness
-- relay there is no such thing as a global BillShield job.
CREATE POLICY p_self_job_outbox ON billshield.job_outbox
    FOR ALL
    USING (user_id = ref.current_app_user())
    WITH CHECK (user_id = ref.current_app_user());

COMMENT ON POLICY p_self_extraction_run ON billshield.extraction_run IS
    'Parent-derived through billshield.bill. The chain is resolved explicitly '
    'rather than leaning on the parent table''s own policy to filter it.';
COMMENT ON POLICY p_self_charge_candidate ON billshield.charge_candidate IS
    'Parent-derived through extraction_run -> bill, resolved in one join.';
COMMENT ON POLICY p_self_promotion_candidate ON billshield.promotion_candidate IS
    'Parent-derived through extraction_run -> bill, resolved in one join.';

-- =============================================================================
-- PRIVILEGES
--
-- No ALTER DEFAULT PRIVILEGES for this schema, ever: a future BillShield table
-- must be granted deliberately, in the migration that creates it, or it arrives
-- with nothing. That is the opposite of the nine older schemas and it is
-- intentional.
--
-- The revokes are written even though no default privilege reaches this schema,
-- because the grant that follows should be the whole answer rather than the
-- visible part of one.
-- =============================================================================

GRANT USAGE ON SCHEMA billshield TO onyx_app_rw;

REVOKE ALL ON billshield.provider, billshield.provider_category,
              billshield.bill, billshield.extraction_run,
              billshield.charge_candidate, billshield.promotion_candidate,
              billshield.job_outbox
    FROM PUBLIC, onyx_app_rw, onyx_app_ro;

-- ---- Global catalogue: read-only to the runtime -------------------------------
GRANT SELECT ON billshield.provider TO onyx_app_rw;
GRANT SELECT ON billshield.provider_category TO onyx_app_rw;

-- ---- Bill: read, create, and a COLUMN-SCOPED update ---------------------------
-- The API creates a bill by naming its owner and nothing else: `id` and
-- `storage_key` are generated, `status` defaults, and every artifact fact
-- arrives at upload completion.
GRANT SELECT ON billshield.bill TO onyx_app_rw;
GRANT INSERT (user_id) ON billshield.bill TO onyx_app_rw;
-- UPDATE is enumerated column by column.
--
-- PRESENT because the API legitimately writes them: `status` and `row_version`
-- move through the lifecycle, `deleted_at` records the customer's request, and
-- the four artifact facts are present precisely because upload completion moves
-- them from unset to finalized EXACTLY ONCE — the trigger, not the grant, is
-- what stops them changing afterwards.
--
-- ABSENT deliberately: `id`, `user_id` and the generated `storage_key`, which
-- are immutable identity; `erased_at`, because only the privacy worker may
-- claim erasure happened; `created_at`; and `updated_at`, which the
-- `ref.set_updated_at()` trigger owns — a runtime that could write it could
-- backdate its own edit.
GRANT UPDATE (status, file_sha256, byte_size, artifact_format, page_count,
              deleted_at, row_version)
    ON billshield.bill TO onyx_app_rw;

-- ---- Extraction results: read-only to the API ---------------------------------
-- The API serves review screens; the worker that WRITES these arrives in Slice 3
-- with its own enumerated grants.
GRANT SELECT ON billshield.extraction_run TO onyx_app_rw;
GRANT SELECT ON billshield.charge_candidate TO onyx_app_rw;
GRANT SELECT ON billshield.promotion_candidate TO onyx_app_rw;

-- ---- Outbox: COLUMN-SCOPED enqueue, and nothing else --------------------------
-- A table-level INSERT would let the request path name its own `claim_state`,
-- mint a `claim_token`, or insert a row that is already `completed`. Enqueue
-- authority is therefore exactly the four intent columns; every operational
-- column is owned by its server default and is not grantable to the API.
--
-- `SELECT (id)` is granted deliberately and narrowly: SQLAlchemy's ORM insert
-- fetches the server-generated primary key with RETURNING, and RETURNING is a
-- SELECT on the returned columns. Without it the enqueue write would have to
-- avoid RETURNING entirely. Nothing else on this table is readable by the API —
-- not the claim state, not the token, not the worker id.
GRANT INSERT (user_id, bill_id, task_code, dedupe_key)
    ON billshield.job_outbox TO onyx_app_rw;
GRANT SELECT (id) ON billshield.job_outbox TO onyx_app_rw;

-- ---- Everyone else -----------------------------------------------------------
-- `onyx_app_ro`: nothing. A reporting role gets a table when somebody names the
-- report (66_legal_acceptance.sql:113-126).
-- `onyx_kb_admin`: nothing. It authors TAX knowledge; commercial-provider
-- administration is a different authority and arrives with the catalogue slice.
-- `onyx_audit_writer`: nothing; it inserts into audit.* only.
-- `onyx_billshield_worker`: nothing, deliberately — see the role comment above.
-- PUBLIC: nothing, revoked above.
