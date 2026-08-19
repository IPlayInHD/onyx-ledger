"""0072_reference_data_period — applies backend/db/sql/07_rules.sql

Entry: Consolidated P0 reference-data integration.

WHY A PERIOD AT ALL. The B04 ingestion produced a real CRA source the governed
model could not hold: prescribed interest rates are announced per calendar
quarter — "in effect from July 1, 2026 to September 30, 2026" — and
`rules.calc_constant` identified a value by (code, tax_year) alone. Four
quarters of one year collide on that key. The three ways out without a schema
change were all wrong: encode the quarter into the code (puts data in the key,
and the code stops naming a semantic), publish one quarter as the annual value
(false for the other nine months), or drop the source (the product needs
prescribed rates). So the period is stored as what it is.

ANNUAL DATA IS UNTOUCHED AND UNREINTERPRETED. Both columns are nullable and
every existing row keeps NULL/NULL, which reads exactly as it always did: the
value for the whole tax year. No backfill, no rewrite, no reinterpretation of a
single existing row.

WHY EXCLUDE RATHER THAN A WIDER UNIQUE. `UNIQUE (code, tax_year,
effective_from)` would permit two overlapping periods that merely start on
different days — precisely the contradiction worth preventing, and one an
author could introduce without noticing. The EXCLUDE constraint says the actual
rule: for one code in one tax year, no two rows may cover overlapping dates.
It also subsumes the old unique constraint, because an annual row's range is
(-infinity, infinity) and two of them overlap. And it correctly refuses an
annual row sitting beside a period row for the same code and year, which is a
contradiction rather than a refinement.

`btree_gist` is required for the scalar `=` operators inside a GiST exclusion.
It is a trusted extension, so this needs no superuser, and the repository
already creates four extensions in db/sql/00_extensions_roles.sql.

THE SNAPSHOT KEY IS DELIBERATELY NOT CHANGED HERE. `ioe.rule_snapshot_artifact`
materialises calc constants under `artifact_key = "{code}:{tax_year}"`, and
`SnapshotService.verify()` recomputes and compares by that key. Widening the key
for annual rows would make every historical snapshot report drifted. The
application therefore keeps the annual key and content byte-identical and only
extends both for rows that actually carry a period; see
`app/services/ioe/snapshot/service.py`.
"""
from alembic import op

revision = "0072_reference_data_period"
down_revision = "0071_knowledge_authoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("""
        ALTER TABLE rules.calc_constant
            ADD COLUMN IF NOT EXISTS effective_from date,
            ADD COLUMN IF NOT EXISTS effective_to   date
    """)
    op.execute("""
        ALTER TABLE rules.calc_constant
            DROP CONSTRAINT IF EXISTS ck_calc_constant_period_ordered
    """)
    op.execute("""
        ALTER TABLE rules.calc_constant
            ADD CONSTRAINT ck_calc_constant_period_ordered CHECK (
                effective_from IS NULL OR effective_to IS NULL
                OR effective_from <= effective_to)
    """)
    # Replaced, not merely supplemented: the exclusion below is strictly
    # stronger than the unique constraint it stands in for.
    op.execute("""
        ALTER TABLE rules.calc_constant
            DROP CONSTRAINT IF EXISTS calc_constant_code_tax_year_key
    """)
    op.execute("""
        ALTER TABLE rules.calc_constant
            DROP CONSTRAINT IF EXISTS ex_calc_constant_no_overlap
    """)
    op.execute("""
        ALTER TABLE rules.calc_constant
            ADD CONSTRAINT ex_calc_constant_no_overlap EXCLUDE USING gist (
                code WITH =,
                tax_year WITH =,
                daterange(effective_from, effective_to, '[]') WITH &&)
    """)


def downgrade() -> None:
    # Restoring UNIQUE (code, tax_year) would fail against any period data this
    # migration made storable, so the period rows go first. Dropping rows is
    # normally forbidden in a downgrade; here the alternative is a downgrade
    # that cannot complete, and the rows being removed are exactly the ones the
    # older schema had no way to represent.
    op.execute("""
        DELETE FROM rules.calc_constant
        WHERE effective_from IS NOT NULL OR effective_to IS NOT NULL
    """)
    op.execute("""
        ALTER TABLE rules.calc_constant
            DROP CONSTRAINT IF EXISTS ex_calc_constant_no_overlap
    """)
    op.execute("""
        ALTER TABLE rules.calc_constant
            DROP CONSTRAINT IF EXISTS ck_calc_constant_period_ordered
    """)
    op.execute("""
        ALTER TABLE rules.calc_constant
            ADD CONSTRAINT calc_constant_code_tax_year_key UNIQUE (code, tax_year)
    """)
    op.execute("""
        ALTER TABLE rules.calc_constant
            DROP COLUMN IF EXISTS effective_to,
            DROP COLUMN IF EXISTS effective_from
    """)
