"""0056_audit_auth_deident — applies backend/db/sql/50_audit_auth_deidentification.sql

Entry 11B6. Installs the audit and authentication de-identification mechanism
for PD-15 and PD-3 through the authoritative path. Until this revision the
objects existed only for `scripts/apply_schema.sh`, which globs `db/sql/*.sql`
— so a database built by Alembic, which is how the migration smoke and the
schema-drift gates build one, did not have them at all.

WHAT IT INSTALLS. Two subject mappings — `identity.account_subject`, which
resolves an audit row's subject while the account lives, and
`identity.deletion_subject`, the tombstone that outlives it — plus the
`identity.deidentify_audit_auth` keyhole, the completion guard
`identity.count_attributable_audit_auth`, `identity.coarsen_ip`, and the
`subject_key` columns on the audit and authentication surfaces.

The upgrade applies the same file `apply_schema.sh` does, so there is one
definition of this behaviour and not two that can drift.

DOWNGRADE IS A DEVELOPMENT ROLLBACK AND IS NOT PRIVACY-SAFE. Dropping
`identity.account_subject` and `identity.deletion_subject` destroys the mapping
that made de-identification meaningful, and dropping `subject_key` from the
audit surfaces discards the only correlation a de-identified history has left.
It does not resurrect a deleted person's identity — the plaintext is gone from
`login_event` and cannot come back — but a database rolled back past this
revision has lost the record of which retained events belonged together. Do not
present this as a way to undo a de-identification.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0056_audit_auth_deident"
down_revision = "0055_lifecycle_worker_acl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("50_audit_auth_deidentification.sql")


def downgrade() -> None:
    # See the module docstring: development rollback only.
    op.execute("DROP FUNCTION IF EXISTS "
               "identity.count_attributable_audit_auth(uuid)")
    op.execute("DROP FUNCTION IF EXISTS "
               "identity.deidentify_audit_auth(uuid, uuid, text)")
    op.execute("DROP TABLE IF EXISTS identity.deletion_subject")
    op.execute("DROP TABLE IF EXISTS identity.account_subject")
    op.execute("ALTER TABLE identity.login_event "
               "DROP COLUMN IF EXISTS deidentified_at")
    for table in ("identity.login_event", "audit.consent_log",
                  "audit.security_event", "audit.audit_log"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS subject_key")
    # coarsen_ip last: the columns above do not depend on it, but a stored
    # generated column added later might, and dropping it first would fail
    # for a reason unrelated to this revision.
    op.execute("DROP FUNCTION IF EXISTS identity.coarsen_ip(inet)")
