"""0046_audit_secret_redaction — applies backend/db/sql/40_audit_secret_redaction.sql

Entry 11A, narrow fix for an active defect found during the privacy audit:
`audit.log_change` copied whole rows, so every registration and password change
wrote the account's Argon2 hash into `audit.audit_log` — an append-only table
with no foreign key to the user and therefore no deletion path.

The named credential columns now have their VALUE replaced with a marker. The
same tables are audited, the same operations, the same record shape.

FUNCTION-ONLY. No table, column, index, constraint, policy, grant or row is
touched. Audit rows already written are not modified — the log is append-only,
and rows that already contain a hash keep it. Removing those requires the
privileged erasure path specified for Entry 11B.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0046_audit_secret_redaction"
down_revision = "0045_admission_preauth_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("40_audit_secret_redaction.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
