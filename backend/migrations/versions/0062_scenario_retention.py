"""0062_scenario_retention — applies backend/db/sql/56_scenario_retention.sql

Entry 11B6E. Adds the SCENARIO_RETENTION lifecycle phase: delete the scenarios
that never sealed a result, clear `label` and `note` on the ones that did, and
refuse to record the phase complete while either is outstanding.

Migration 0060 detached `ioe.scenario` from `identity.user_account` so sealed
scenarios would survive account removal. It cannot distinguish a sealed
scenario from a draft, so drafts survive too — measured at the time, a
`pending` scenario carrying free text outlived its owner. 0060 recorded that as
BRANCH_CLEANUP_PENDING; this closes it.

A NEW PHASE RATHER THAN A WIDER SOURCE_DATA. `count_remaining_source_data`
names eight live financial, profile and wealth tables and no scenario table.
Widening it would change what an already-recorded COMPLETE means for every
account that has finished it.

DOWNGRADE restores the previous phase vocabulary and the pre-11B6E completion
function, and is refused once the phase has been used. Schema reversal and data
re-identification are different things: deleted drafts are not restorable and
cleared free text is not recoverable, so a downgrade that ran after the phase
had done its work would leave a database claiming a phase does not exist while
its irreversible effects remain.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0062_scenario_retention"
down_revision = "0061_audit_auth_phase_complete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("56_scenario_retention.sql")


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.exec_driver_sql(
        "SELECT count(*) FROM identity.account_lifecycle_phase"
        " WHERE phase = 'SCENARIO_RETENTION'"
    ).scalar_one()
    if used:
        raise RuntimeError(
            "0062_scenario_retention cannot be downgraded: the phase has run "
            f"for {used} account(s). It deletes unsealed scenarios and clears "
            "user free text on retained ones, and neither is recoverable. "
            "Removing the phase now would leave a schema that denies the phase "
            "exists while its irreversible effects remain in the data."
        )

    op.execute("ALTER TABLE identity.account_lifecycle_phase "
               "DROP CONSTRAINT IF EXISTS ck_lifecycle_phase_name")
    op.execute("ALTER TABLE identity.account_lifecycle_phase "
               "ADD CONSTRAINT ck_lifecycle_phase_name "
               "CHECK (phase IN ('SOURCE_DATA', 'DOCUMENTS', "
               "'AUDIT_AUTH_DEIDENTIFICATION'))")
    op.execute("DROP FUNCTION IF EXISTS "
               "identity.prepare_scenario_retention(uuid, uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS "
               "identity.count_remaining_scenario_privacy_work(uuid)")
    # Restores `complete_lifecycle_phase` without the SCENARIO_RETENTION branch.
    apply_sql_file("55_audit_auth_phase_completion.sql")
