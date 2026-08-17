-- =============================================================================
-- Entry: Tax Knowledge Source Registry + Provenance Foundation (migration 0070)
-- =============================================================================
--
-- WHAT THIS IS. The governed answer to "exactly which authoritative source, and
-- which part of it, supports this Onyx tax rule, formula or reference-data
-- value?" Four tables:
--
--   tax_kb.tax_source          the continuing publication (Income Tax Act,
--                              CRA Guide T4044) — identity that outlives editions
--   tax_kb.tax_source_version  one edition/consolidation actually retrieved,
--                              with its content fingerprint
--   tax_kb.source_citation     a source version PLUS a structured locator —
--                              section, subsection, table, form line
--   tax_kb.knowledge_citation  which governed knowledge object a citation
--                              supports
--
-- WHAT IT IS NOT. It is not tax logic. A source document is evidence for an
-- interpretation; the structured rule/formula/reference-data model remains the
-- runtime authority. Nothing here is consulted by RulesEvaluatorService or
-- TaxEngineService to decide eligibility or compute tax.
--
-- WHY THE EXISTING STUBS WERE NOT EXTENDED. `tax_kb.gov_source` is (name, url)
-- and `tax_kb.legislation_reference` is a single free-form `citation` text
-- column; a rule version points at ONE of each through nullable FKs, and the
-- loader returns a one-tuple. Neither carries a version, an issuer, a
-- jurisdiction, an effective period, a fingerprint or a supersession edge, so
-- there is nothing to hang provenance on: editing a shared `gov_source` row
-- silently rewrites what every rule referencing it claims to be based on. They
-- are left untouched and unused rather than mutated into something they are
-- not.
--
-- IMMUTABILITY, AND HOW SUPERSESSION AVOIDS IT. A new edition never updates the
-- old row. The SUCCESSOR points backwards through `supersedes_version_id`, so
-- "S1 was superseded" is derived from S2's existence rather than written into
-- S1. That is what lets a historical rule keep resolving the exact bytes it was
-- authored against while the world moves on — the retention checkpoint chain
-- uses the same shape for the same reason.
--
-- NO RLS, DELIBERATELY. Tax law is global reference knowledge, not tenant data;
-- every existing tax_kb table has row security disabled and this follows that
-- pattern rather than inventing tenancy for public statutes. Least privilege is
-- enforced through GRANTS instead — see the bottom of this file, which is the
-- security-relevant part.

-- ---------------------------------------------------------------------------
-- The continuing publication.
-- ---------------------------------------------------------------------------
CREATE TABLE tax_kb.tax_source (
    id                   uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    -- Closed vocabulary. Kept explicit rather than ranked: the registry records
    -- WHAT KIND of authority is being cited so a later review can judge it. It
    -- deliberately does NOT compute a legal precedence order — nothing in this
    -- repository governs that today, and inventing one would be a tax opinion
    -- dressed as an enum.
    source_type          text NOT NULL CHECK (source_type IN (
                             'STATUTE',
                             'REGULATION',
                             'GOVERNMENT_GUIDANCE',
                             'CRA_FOLIO',
                             'CRA_GUIDE',
                             'FORM',
                             'FORM_INSTRUCTIONS',
                             'SCHEDULE',
                             'RATE_TABLE',
                             'INDEXED_PARAMETER_PUBLICATION',
                             -- Registrable for research, and distinguishable so
                             -- publication policy can refuse it later. A blog or
                             -- an LLM transcript may be recorded; it must never
                             -- be mistakable for a statute.
                             'SECONDARY_COMMENTARY')),
    -- The issuing authority, as a controlled code rather than free text so
    -- "CRA" and "Canada Revenue Agency" cannot become two publishers.
    issuer_code          text NOT NULL CHECK (issuer_code IN (
                             'PARLIAMENT_OF_CANADA',
                             'GOVERNMENT_OF_CANADA',
                             'DEPARTMENT_OF_FINANCE_CANADA',
                             'CANADA_REVENUE_AGENCY',
                             'PROVINCIAL_LEGISLATURE',
                             'PROVINCIAL_TAX_AUTHORITY',
                             'OTHER')),
    -- THE existing jurisdiction authority, reused. A federal source and an
    -- Ontario source are different rows and cannot satisfy each other.
    jurisdiction_id      uuid NOT NULL REFERENCES ref.jurisdiction(id),
    -- The publisher's own stable identifier: "R.S.C. 1985, c. 1 (5th Supp.)",
    -- "T4044", "S1-F1-C1". Part of identity, so two editions of one guide are
    -- one source.
    official_identifier  text NOT NULL,
    title                text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    -- Semantic identity. The same identifier issued by a different authority or
    -- for a different jurisdiction is a different source, not a duplicate.
    CONSTRAINT uq_tax_source_identity
        UNIQUE (jurisdiction_id, issuer_code, official_identifier)
);

COMMENT ON TABLE tax_kb.tax_source IS
    'A continuing authoritative tax publication, identified independently of '
    'any single edition. Global reference knowledge, never tenant data.';

-- ---------------------------------------------------------------------------
-- One retrieved edition.
-- ---------------------------------------------------------------------------
CREATE TABLE tax_kb.tax_source_version (
    id                    uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    source_id             uuid NOT NULL REFERENCES tax_kb.tax_source(id),
    -- The publisher's edition label, when it has one ("2024 edition",
    -- "consolidated to 2024-06-20").
    edition               text NOT NULL,
    -- FOUR DIFFERENT DATES, kept apart on purpose (§7). A guide published in
    -- one year can apply to another tax year, take legal effect on a third
    -- date, and be retrieved by Onyx on a fourth. Collapsing them is how a
    -- source silently gets applied to the wrong year.
    --
    -- `tax_year` is NULLABLE and that is the point: a statute's applicability is
    -- genuinely date-based, and forcing a year onto it would be an invented
    -- fact. Year-scoped publications (rate tables, indexed parameters) set it.
    tax_year              ref.tax_year_num,
    publication_date      date,
    effective_from        date,
    effective_to          date,
    retrieved_at          timestamptz NOT NULL,
    -- Where the edition was obtained. Recorded, never fetched at runtime.
    official_locator      text NOT NULL,
    -- Content identity. See app/services/tax_kb/sources/fingerprint.py for the
    -- documented canonical rule; `fingerprint_method` names WHICH rule produced
    -- this digest, so a raw-bytes digest can never be mistaken for a digest of
    -- extracted text.
    content_fingerprint   text NOT NULL,
    fingerprint_method    text NOT NULL CHECK (fingerprint_method IN (
                              'RAW_BYTES_SHA256',
                              'DECLARED_BY_OPERATOR')),
    -- The publisher's classification of this edition AS RETRIEVED. Set once and
    -- never updated. Whether a newer edition exists is a different question,
    -- answered by the supersession edge below rather than by mutating this.
    status                text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN (
                              'ACTIVE', 'WITHDRAWN')),
    -- Supersession points BACKWARD from the successor, so no historical row is
    -- ever rewritten. A version is superseded exactly when another version
    -- names it — derived, and therefore never stale.
    supersedes_version_id uuid REFERENCES tax_kb.tax_source_version(id),
    -- The manifest contract that produced this row, so a future manifest shape
    -- change cannot silently reinterpret rows written under the old one.
    manifest_schema_version text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    -- IDEMPOTENCY (§24). Identity is the source plus its content — not the URL
    -- query string, not the local file path, not when it was fetched. Importing
    -- the same bytes for the same source twice lands here and returns the row
    -- that already exists.
    CONSTRAINT uq_tax_source_version_content
        UNIQUE (source_id, content_fingerprint),
    -- A linear chain: at most one successor per predecessor, so "what replaced
    -- this" has one answer.
    CONSTRAINT uq_tax_source_version_supersedes
        UNIQUE (supersedes_version_id),
    CONSTRAINT ck_tax_source_version_effective_order
        CHECK (effective_to IS NULL OR effective_from IS NULL
               OR effective_to >= effective_from),
    -- A version cannot supersede itself.
    CONSTRAINT ck_tax_source_version_not_self_superseding
        CHECK (supersedes_version_id IS NULL OR supersedes_version_id <> id)
);

CREATE INDEX ix_tax_source_version_source
    ON tax_kb.tax_source_version (source_id, created_at DESC, id DESC);

COMMENT ON TABLE tax_kb.tax_source_version IS
    'One retrieved edition of a tax source, immutable once written. Supersession '
    'is recorded by the successor pointing back, so historical provenance is '
    'never rewritten when a newer edition appears.';

-- ---------------------------------------------------------------------------
-- A precise location inside one edition.
-- ---------------------------------------------------------------------------
CREATE TABLE tax_kb.source_citation (
    id                 uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    source_version_id  uuid NOT NULL REFERENCES tax_kb.tax_source_version(id),
    -- The structured locator: section, subsection, paragraph, clause, page,
    -- table, schedule, form_line, heading, anchor. Structured rather than one
    -- free-form string so "s. 118.2(2)(a)" is queryable and comparable instead
    -- of merely printable. Validated against a closed key set by the importer.
    locator            jsonb NOT NULL,
    -- Canonical digest of the locator, so citation identity does not depend on
    -- JSON key order. This is what makes a citation REUSABLE: the same location
    -- in the same edition is the same row, cited by many knowledge objects.
    locator_hash       text NOT NULL,
    -- Optional human-readable rendering. Convenience only — never identity.
    label              text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_source_citation_identity
        UNIQUE (source_version_id, locator_hash),
    CONSTRAINT ck_source_citation_locator_object
        CHECK (jsonb_typeof(locator) = 'object')
);

COMMENT ON TABLE tax_kb.source_citation IS
    'A source version plus a structured locator. Reusable: one citation may '
    'support many governed knowledge objects.';

-- ---------------------------------------------------------------------------
-- What the citation supports.
--
-- AN EXCLUSIVE ARC RATHER THAN A POLYMORPHIC KEY. One nullable FK per referent
-- kind with exactly one required, so every link keeps real referential
-- integrity. A (kind, object_id) pair would have been one column shorter and
-- would have let a citation point at a formula that no longer exists.
-- ---------------------------------------------------------------------------
CREATE TABLE tax_kb.knowledge_citation (
    id                    uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    citation_id           uuid NOT NULL REFERENCES tax_kb.source_citation(id),
    -- Rule versions. Deadlines and required documents inherit through here:
    -- they hang off a rule version and have no independent existence, so a
    -- separate link would be a second answer to the same question.
    rule_version_id       uuid REFERENCES tax_kb.tax_rule_version(id),
    -- Formulas need their OWN provenance: a formula is shared across rules, so
    -- "the rule that uses it" is ambiguous the moment two do.
    formula_id            uuid REFERENCES rules.calc_formula(id),
    -- Reference data needs its own too, and more urgently: brackets, limits and
    -- indexed parameters are read by the engine directly and never pass through
    -- a rule version at all, so nothing could be inherited.
    tax_bracket_set_id    uuid REFERENCES tax_kb.tax_bracket_set(id),
    contribution_limit_id uuid REFERENCES tax_kb.contribution_limit(id),
    benefit_parameter_id  uuid REFERENCES tax_kb.benefit_parameter(id),
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_knowledge_citation_exactly_one_subject CHECK (
        num_nonnulls(rule_version_id, formula_id, tax_bracket_set_id,
                     contribution_limit_id, benefit_parameter_id) = 1)
);

CREATE INDEX ix_knowledge_citation_rule_version
    ON tax_kb.knowledge_citation (rule_version_id) WHERE rule_version_id IS NOT NULL;
CREATE INDEX ix_knowledge_citation_formula
    ON tax_kb.knowledge_citation (formula_id) WHERE formula_id IS NOT NULL;
CREATE INDEX ix_knowledge_citation_citation
    ON tax_kb.knowledge_citation (citation_id);

COMMENT ON TABLE tax_kb.knowledge_citation IS
    'Which governed knowledge object a source citation supports. Exactly one '
    'subject per row, by CHECK, with a real foreign key to it.';

-- ---------------------------------------------------------------------------
-- PRIVILEGES — the security-relevant part of this file.
--
-- `tax_kb` carries DEFAULT PRIVILEGES granting onyx_app_rw arwd, so a new table
-- here would silently hand the CUSTOMER RUNTIME ROLE the ability to insert,
-- rewrite and delete authoritative tax law. Taking that back is not a
-- refinement; it is the point. Ordinary customers read provenance and nothing
-- else.
--
-- The KB authoring role gets INSERT and SELECT but NOT update or delete:
-- corrections happen by registering a new version and superseding, never by
-- editing history. Nothing here is exposed through SECURITY DEFINER, and no
-- application route can write these tables at all.
-- ---------------------------------------------------------------------------
REVOKE INSERT, UPDATE, DELETE ON tax_kb.tax_source          FROM onyx_app_rw;
REVOKE INSERT, UPDATE, DELETE ON tax_kb.tax_source_version  FROM onyx_app_rw;
REVOKE INSERT, UPDATE, DELETE ON tax_kb.source_citation     FROM onyx_app_rw;
REVOKE INSERT, UPDATE, DELETE ON tax_kb.knowledge_citation  FROM onyx_app_rw;

GRANT SELECT ON tax_kb.tax_source, tax_kb.tax_source_version,
                tax_kb.source_citation, tax_kb.knowledge_citation
    TO onyx_app_ro;

GRANT SELECT, INSERT ON tax_kb.tax_source, tax_kb.tax_source_version,
                        tax_kb.source_citation, tax_kb.knowledge_citation
    TO onyx_kb_admin;
