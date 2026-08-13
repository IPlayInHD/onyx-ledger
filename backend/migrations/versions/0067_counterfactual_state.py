"""0067_counterfactual_state — applies backend/db/sql/61_counterfactual_derived_state.sql

Entry 12B1. Adds two columns to `ioe.scenario_result` so a sealed scenario
carries what its counterfactual tax state CONTAINED, not only what its total
was: the TaxEngineService line items it already computed and discarded, and the
pinned-rule candidate set it never evaluated.

COLUMNS RATHER THAN A TABLE, on measured evidence. `pg_column_size` over the
real canonical payload: 1 candidate → 1,808 bytes stored; 25 → 2,225; 500 →
21,378 against 436,640 uncompressed. TOAST compresses the repeated keys to under
5% at scale. The read path hashes the artifact as a unit and never queries
inside it, so child rows would buy queryability nobody needs and pay a 71st
privacy-bearing table for it.

The new columns inherit `trg_immutable` (`reject_result_mutation`) and
`trg_write_cutoff` from the table, both row-level and therefore already covering
columns added afterwards — verified against `pg_trigger`, not read off a
migration. So the privacy universe stays at 70 tables with no new policy, no new
trigger and no new classification.

LEGACY ROWS ARE UNTOUCHED and stay NULL in both columns. The CHECK permits
"neither" precisely so that pre-12B1 scenarios remain legal; backfilling them
against today's rules would fabricate historical evidence.

DOWNGRADE drops the constraint and the two columns. Data loss is limited to
sealed derived state that only exists for scenarios created after this
migration, and those scenarios' own result hashes were computed with it — so a
downgraded database can no longer verify them, which the integrity model already
reports as unavailable rather than as a mismatch.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0067_counterfactual_state"
down_revision = "0066_terminal_account_removal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("61_counterfactual_derived_state.sql")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE ioe.scenario_result "
        "DROP CONSTRAINT IF EXISTS ck_counterfactual_derived_state_paired"
    )
    op.execute(
        "ALTER TABLE ioe.scenario_result "
        "DROP COLUMN IF EXISTS counterfactual_derived_state_hash"
    )
    op.execute(
        "ALTER TABLE ioe.scenario_result "
        "DROP COLUMN IF EXISTS counterfactual_derived_state"
    )
