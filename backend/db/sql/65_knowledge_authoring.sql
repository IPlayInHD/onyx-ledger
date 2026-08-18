-- =============================================================================
-- Onyx Ledger — 65 · Governed tax-knowledge authoring + publication pipeline
-- Alembic revision: 0071_knowledge_authoring
--
-- No parallel rule model. The draft representation is the rule version the
-- repository already has, in a non-published lifecycle state, and publication is
-- the flip the TKMS lifecycle already performs. Duplicating tax_rule_version and
-- its eight child tables into a "draft" mirror would create two models of the
-- same knowledge, evolving independently, and the second one would eventually
-- be the one that disagreed with the engine.
--
-- What is genuinely missing is smaller than a schema and is added here:
--
--   1. ref.deadline_type — the governed deadline taxonomy. rules.rule_deadline
--      .deadline_code was free text with no constraint and no vocabulary, so
--      "known deadline code" was not a question anything could answer.
--
--   2. A validated specification's IDENTITY on the validation report, so
--      publication can bind to the exact draft that was validated rather than
--      to a report that merely names the same row. Without it a draft could be
--      validated, edited, and published carrying its predecessor's approval.
--
--   3. The same identity plus a release identity on the publication record, so
--      "what published, under which policy, together with what" is answerable
--      from stored rows.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1 · The deadline taxonomy
--
-- Categories, never dates. A code says WHAT KIND of deadline a rule is talking
-- about; the date itself is authored per rule version and carries its own
-- citation. Seeding a date here would be publishing tax law from a migration.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.deadline_type (
    code        text PRIMARY KEY,
    label       text NOT NULL,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref.deadline_type IS
    'Governed vocabulary of deadline KINDS. The date a deadline falls on is '
    'authored per rule version with its own source citation; this table only '
    'says what sort of deadline it is.';

INSERT INTO ref.deadline_type (code, label, description) VALUES
    ('T1_FILING',            'Personal return filing',
     'Deadline to file a personal income tax return for the year.'),
    ('T1_FILING_SELF_EMPLOYED', 'Self-employed return filing',
     'Filing deadline where the taxpayer or spouse carried on a business.'),
    ('BALANCE_DUE',          'Balance owing payment',
     'Deadline to pay a balance owing for the year.'),
    ('INSTALMENT_PAYMENT',   'Instalment payment',
     'Deadline for a required tax instalment.'),
    ('CONTRIBUTION',         'Contribution deadline',
     'Deadline to contribute to a registered plan and have it count for the year.'),
    ('ELECTION_FILING',      'Election filing',
     'Deadline to file a tax election.'),
    ('ADJUSTMENT_REQUEST',   'Adjustment request',
     'Deadline to request a change to a filed return.'),
    ('OBJECTION',            'Notice of objection',
     'Deadline to object to an assessment or reassessment.'),
    ('INFORMATION_RETURN',   'Information return',
     'Deadline to file an information return or slip.'),
    ('EXPENSE_INCURRED',     'Expense incurred by',
     'Date by which an expense must be incurred to be claimable for the year.')
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 1b · Executable examples, stored WITH the knowledge they verify
--
-- A production rule publishes with cases that were actually run. Keeping them
-- only in the submitted manifest would mean nobody can re-run a published
-- rule's own examples afterwards, and the specification rebuilt from stored
-- rows would be missing something the author wrote — which the publication
-- hash binding would then read as a draft that had changed.
--
-- They are NOT runtime authority. The evaluator never reads these tables; they
-- verify authored knowledge and nothing else.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rules.rule_example (
    id                      uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    rule_version_id         uuid NOT NULL
                            REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    name                    text NOT NULL,
    -- Facts as TEXT values. A JSON number is a float, and an example whose
    -- threshold shifted by binary rounding would verify the wrong rule.
    facts                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    expect_eligible         boolean NOT NULL,
    expected_outcome_types  jsonb NOT NULL DEFAULT '[]'::jsonb,
    sort_order              smallint NOT NULL DEFAULT 0,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_version_id, name)
);
CREATE INDEX IF NOT EXISTS ix_rule_example_version
    ON rules.rule_example (rule_version_id, sort_order);

COMMENT ON TABLE rules.rule_example IS
    'Governed test cases for one rule version. Verifies authored knowledge; '
    'never consulted at runtime and never an eligibility authority.';

CREATE TABLE IF NOT EXISTS rules.formula_vector (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    formula_id  uuid NOT NULL REFERENCES rules.calc_formula(id) ON DELETE CASCADE,
    name        text NOT NULL,
    inputs      jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- `numeric` without a scale: a test vector's expected value is compared
    -- EXACTLY, so a column that rounded it would make the comparison a lie.
    expected    numeric NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (formula_id, name)
);
CREATE INDEX IF NOT EXISTS ix_formula_vector_formula
    ON rules.formula_vector (formula_id);

COMMENT ON TABLE rules.formula_vector IS
    'Deterministic input→exact-output vectors for one formula. Compared with '
    'exact decimal equality; there is no tolerance anywhere in this pipeline.';

-- ---------------------------------------------------------------------------
-- 1bb · A citation subject the registry was missing
--
-- `tax_kb.knowledge_citation` covers rule versions, formulas, bracket sets,
-- contribution limits and benefit parameters. It does not cover
-- `rules.calc_constant` — and a calc constant is read by the engine DIRECTLY
-- and pinned by `ioe.rule_snapshot_artifact`, which makes it precisely the case
-- that cannot inherit provenance from anything.
--
-- The authoring pipeline requires a citation for every reference-data object,
-- so without this a constant would be required to carry provenance the schema
-- had nowhere to record: required and unrecorded is worse than either.
--
-- Additive: one nullable column, and the exclusive-arc CHECK is widened to
-- count it. Every existing row still satisfies exactly-one.
-- ---------------------------------------------------------------------------
ALTER TABLE tax_kb.knowledge_citation
    ADD COLUMN IF NOT EXISTS calc_constant_id uuid
        REFERENCES rules.calc_constant(id);

ALTER TABLE tax_kb.knowledge_citation
    DROP CONSTRAINT IF EXISTS ck_knowledge_citation_exactly_one_subject;
ALTER TABLE tax_kb.knowledge_citation
    ADD CONSTRAINT ck_knowledge_citation_exactly_one_subject CHECK (
        num_nonnulls(rule_version_id, formula_id, tax_bracket_set_id,
                     contribution_limit_id, benefit_parameter_id,
                     calc_constant_id) = 1);

CREATE INDEX IF NOT EXISTS ix_knowledge_citation_calc_constant
    ON tax_kb.knowledge_citation (calc_constant_id)
    WHERE calc_constant_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 1c · The supersession an author DECLARES
--
-- `superseded_by_version_id` already exists and records what publication DID:
-- it is written on the predecessor at the moment a successor takes its place.
-- That is a fact about an event, and it does not exist yet while a draft is
-- being reviewed.
--
-- This column is the other half — a forward pointer on the SUCCESSOR saying
-- what its author intends to replace, present from the moment the draft is
-- staged. A reviewer approving a correction can therefore see what it corrects,
-- and the specification rebuilt from stored rows carries the declaration the
-- author made rather than losing it.
--
-- Same shape the source registry uses: the successor names its predecessor, so
-- no historical row is rewritten to record a relationship.
-- ---------------------------------------------------------------------------
ALTER TABLE tax_kb.tax_rule_version
    ADD COLUMN IF NOT EXISTS supersedes_version_id uuid
        REFERENCES tax_kb.tax_rule_version(id);

COMMENT ON COLUMN tax_kb.tax_rule_version.supersedes_version_id IS
    'What this version''s author declared it replaces. Distinct from '
    'superseded_by_version_id, which records what publication actually did to '
    'the predecessor.';

CREATE INDEX IF NOT EXISTS ix_rule_version_supersedes
    ON tax_kb.tax_rule_version (supersedes_version_id)
    WHERE supersedes_version_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2 · Validated specification identity
--
-- Nullable throughout, and that is load-bearing rather than lax. Every report
-- written before this migration has NULL here, and NULL must read as "this was
-- not a governed publishability assessment" — never as "assessed and fine". The
-- governed publication path requires the columns to be populated, so a legacy
-- report cannot satisfy it by being silent.
-- ---------------------------------------------------------------------------
ALTER TABLE tkms.validation_report
    ADD COLUMN IF NOT EXISTS spec_hash            text,
    ADD COLUMN IF NOT EXISTS spec_schema_version  text,
    ADD COLUMN IF NOT EXISTS policy_version       text,
    ADD COLUMN IF NOT EXISTS publishable          boolean,
    ADD COLUMN IF NOT EXISTS readiness            jsonb,
    ADD COLUMN IF NOT EXISTS error_codes          jsonb;

COMMENT ON COLUMN tkms.validation_report.spec_hash IS
    'Domain-separated hash of the exact specification this report assessed. '
    'Publication binds to it, so a draft edited after validation cannot publish '
    'carrying its predecessor''s approval. NULL means the report predates the '
    'governed pipeline and can never authorize a governed publication.';
COMMENT ON COLUMN tkms.validation_report.publishable IS
    'The hard verdict, decided by ERROR findings alone. NULL is not false and '
    'not true: it means the question was never asked.';
COMMENT ON COLUMN tkms.validation_report.readiness IS
    'Per-family readiness. MISSING, NOT_APPLICABLE and INVALID are kept apart '
    'so an unassessed family never reads as an empty success.';

-- One governed verdict per (version, spec_hash). Revalidating an UNCHANGED
-- draft is idempotent instead of accumulating reports that could disagree, and
-- the publication lookup is a point read rather than "the latest, hopefully".
CREATE UNIQUE INDEX IF NOT EXISTS uq_validation_report_spec
    ON tkms.validation_report (target_version_id, spec_hash)
    WHERE target_version_id IS NOT NULL AND spec_hash IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3 · Publication record
--
-- `pack_hash` is the release concept, and it is deliberately a column rather
-- than a table. What a release must answer is "what published together, and
-- under which policy" — one indexed column answers it. A KnowledgeRelease
-- entity would have added a lifecycle, an owner and a status that nothing
-- reads, which is the ceremony §49 warns against.
-- ---------------------------------------------------------------------------
ALTER TABLE admin.rule_publication
    ADD COLUMN IF NOT EXISTS spec_hash      text,
    ADD COLUMN IF NOT EXISTS pack_hash      text,
    ADD COLUMN IF NOT EXISTS policy_version text,
    ADD COLUMN IF NOT EXISTS channel        text NOT NULL DEFAULT 'legacy';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'rule_publication_channel_check') THEN
        ALTER TABLE admin.rule_publication
            ADD CONSTRAINT rule_publication_channel_check
            CHECK (channel IN ('governed', 'legacy'));
    END IF;
END $$;

COMMENT ON COLUMN admin.rule_publication.channel IS
    'Which path published this version. ''governed'' passed the authoring '
    'pipeline: bound spec hash, qualifying provenance, executed examples. '
    '''legacy'' is the pre-existing import path, still available for fixtures '
    'and migrations. Recorded explicitly rather than inferred from a code '
    'prefix or an environment name.';
COMMENT ON COLUMN admin.rule_publication.pack_hash IS
    'Identity of the coherent publication set this version published in. Equal '
    'across every member of one release; distinct releases never share it.';

CREATE INDEX IF NOT EXISTS ix_rule_publication_pack
    ON admin.rule_publication (pack_hash);
CREATE INDEX IF NOT EXISTS ix_rule_publication_spec
    ON admin.rule_publication (spec_hash);

-- ---------------------------------------------------------------------------
-- 4 · Privileges
--
-- ref.deadline_type is a reference vocabulary: everyone reads it, nobody in the
-- application writes it. `ref` carries no DEFAULT PRIVILEGES for onyx_app_rw
-- (16_rls_grants.sql grants ref only SELECT and sets defaults on the user
-- schemas), so there is no accidental write grant to take back here — but the
-- grants are stated explicitly rather than assumed, which is the lesson the
-- source registry entry paid for.
-- ---------------------------------------------------------------------------
GRANT SELECT ON ref.deadline_type TO onyx_app_rw;
GRANT SELECT ON ref.deadline_type TO onyx_app_ro;
GRANT SELECT ON ref.deadline_type TO onyx_kb_admin;
REVOKE INSERT, UPDATE, DELETE ON ref.deadline_type FROM onyx_app_rw;
REVOKE INSERT, UPDATE, DELETE ON ref.deadline_type FROM onyx_kb_admin;

-- The example tables live in `rules`, which 18_admin_grants.sql gives
-- onyx_app_rw INSERT/UPDATE/DELETE on by DEFAULT PRIVILEGES. Examples are
-- authoring artifacts, so the customer runtime reads them and nothing more —
-- taken back explicitly, because inheriting a write grant is exactly the hazard
-- the source registry entry uncovered.
REVOKE INSERT, UPDATE, DELETE ON rules.rule_example   FROM onyx_app_rw;
REVOKE INSERT, UPDATE, DELETE ON rules.formula_vector FROM onyx_app_rw;
GRANT SELECT ON rules.rule_example, rules.formula_vector TO onyx_app_rw;
GRANT SELECT ON rules.rule_example, rules.formula_vector TO onyx_app_ro;
GRANT SELECT, INSERT ON rules.rule_example, rules.formula_vector
    TO onyx_kb_admin;
