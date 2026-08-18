"""0071_knowledge_authoring — applies backend/db/sql/65_knowledge_authoring.sql

Entry: Governed Tax Rule / Formula Authoring + Publication Pipeline.

NO PARALLEL RULE MODEL. The repository already stores a rule version and its
eight child tables, and the TKMS lifecycle already moves one from draft to
published. A separate "draft" mirror of that model would be a second
representation of the same knowledge, evolving on its own, and the day the two
disagreed the engine would be reading whichever one nobody had checked. So the
draft representation stays the existing rule version in a non-published status,
and this migration adds only what could not otherwise exist.

THE DEADLINE TAXONOMY. `rules.rule_deadline.deadline_code` was free text with no
constraint, no reference table and no vocabulary, so "is this a known deadline
code?" had no answer. `ref.deadline_type` gives it one. The seeded rows are
KINDS of deadline, never dates: a date is tax law, is authored per rule version,
and carries its own citation. A migration that seeded April 30 would be
publishing law from a schema change.

A CITATION SUBJECT THE REGISTRY WAS MISSING. `knowledge_citation` covered rule
versions, formulas, bracket sets, contribution limits and benefit parameters,
but not `rules.calc_constant` — which the engine reads directly and
`ioe.rule_snapshot_artifact` pins, making it exactly the case that cannot
inherit provenance from anything. The pipeline requires a citation for every
reference-data object, so without the column a constant would have to carry
provenance the schema had nowhere to record. Widening the exclusive arc is
additive and every existing row still satisfies exactly-one.

WHY EXAMPLES GET TABLES. A production rule publishes with cases that were
actually run, and §26 says they are versioned with the knowledge or its review
artifact. Left in the submitted manifest they would be unrunnable the moment
publication finished, and the specification rebuilt from stored rows would be
missing something the author wrote — which the hash binding below would then
read as a draft that had changed. `rules.rule_example` and
`rules.formula_vector` are children of the version and formula respectively, so
they cascade exactly as every other child does. Neither is runtime authority:
the evaluator never reads them.

WHY A SPEC HASH ON THE VALIDATION REPORT. Publication previously accepted any
passed report targeting the version — or, failing that, any passed report for
its import job. Neither says the report describes the version's CURRENT content,
so a draft could be validated, edited, and published on the strength of the
validation its predecessor passed. The report now records the domain-separated
hash of exactly what it assessed, and the governed publication path recomputes
that hash from live rows and refuses a mismatch.

The new columns are NULLABLE and that is load-bearing. Every report written
before this migration has NULL, and NULL reads as "this was never a governed
publishability assessment" — never as "assessed and fine". The governed path
requires them populated, so silence cannot authorize anything.

`admin.rule_publication.channel` records WHICH path published a version rather
than inferring production-ness from a code prefix or an environment string, and
`pack_hash` gives a coherent release an identity without a release table whose
lifecycle nothing would read.

DOWNGRADE drops the added columns, the index and the taxonomy table. The loss is
the ability to bind a publication to a validated specification; no sealed
artifact, replayed scenario or tax result references any of it.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0071_knowledge_authoring"
down_revision = "0070_tax_source_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("65_knowledge_authoring.sql")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tkms.uq_validation_report_spec")
    op.execute("DROP INDEX IF EXISTS admin.ix_rule_publication_pack")
    op.execute("DROP INDEX IF EXISTS admin.ix_rule_publication_spec")
    op.execute("""
        ALTER TABLE tkms.validation_report
            DROP COLUMN IF EXISTS spec_hash,
            DROP COLUMN IF EXISTS spec_schema_version,
            DROP COLUMN IF EXISTS policy_version,
            DROP COLUMN IF EXISTS publishable,
            DROP COLUMN IF EXISTS readiness,
            DROP COLUMN IF EXISTS error_codes
    """)
    op.execute("""
        ALTER TABLE admin.rule_publication
            DROP CONSTRAINT IF EXISTS rule_publication_channel_check
    """)
    op.execute("""
        ALTER TABLE admin.rule_publication
            DROP COLUMN IF EXISTS spec_hash,
            DROP COLUMN IF EXISTS pack_hash,
            DROP COLUMN IF EXISTS policy_version,
            DROP COLUMN IF EXISTS channel
    """)
    op.execute("DROP INDEX IF EXISTS tax_kb.ix_knowledge_citation_calc_constant")
    op.execute("""
        ALTER TABLE tax_kb.knowledge_citation
            DROP CONSTRAINT IF EXISTS ck_knowledge_citation_exactly_one_subject
    """)
    op.execute("""
        ALTER TABLE tax_kb.knowledge_citation
            DROP COLUMN IF EXISTS calc_constant_id
    """)
    op.execute("""
        ALTER TABLE tax_kb.knowledge_citation
            ADD CONSTRAINT ck_knowledge_citation_exactly_one_subject CHECK (
                num_nonnulls(rule_version_id, formula_id, tax_bracket_set_id,
                             contribution_limit_id, benefit_parameter_id) = 1)
    """)
    op.execute("DROP INDEX IF EXISTS tax_kb.ix_rule_version_supersedes")
    op.execute("""
        ALTER TABLE tax_kb.tax_rule_version
            DROP COLUMN IF EXISTS supersedes_version_id
    """)
    op.execute("DROP TABLE IF EXISTS rules.rule_example")
    op.execute("DROP TABLE IF EXISTS rules.formula_vector")
    op.execute("DROP TABLE IF EXISTS ref.deadline_type")
