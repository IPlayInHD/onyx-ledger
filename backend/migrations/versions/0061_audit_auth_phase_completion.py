"""0061_audit_auth_phase_completion — applies backend/db/sql/55_audit_auth_phase_completion.sql

Entry 11B6D. `identity.complete_lifecycle_phase` enforced its "nothing in scope
remains" rule for SOURCE_DATA only, so AUDIT_AUTH_DEIDENTIFICATION could be
recorded COMPLETE while the account was still fully attributable through its
login, security and consent history and its live subject mapping. Measured
before the fix: `complete_lifecycle_phase` returned true with
`count_attributable_audit_auth` at 5.

Terminal account removal will be gated on this phase being COMPLETE, so a
status that can lie here is the one that matters most. The branch is added in
the database rather than in the worker, so no caller can assert completion into
being.

DOWNGRADE re-applies 45_source_data_purge.sql, restoring the function to its
pre-11B6D text. That is a genuine schema reversal and reconstructs no identity:
de-identification already performed stays performed, because this migration
changes only when completion may be RECORDED, never what the keyhole did.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0061_audit_auth_phase_complete"
down_revision = "0060_retained_root_detachment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("55_audit_auth_phase_completion.sql")


def downgrade() -> None:
    apply_sql_file("45_source_data_purge.sql")
