"""0048_audit_payload_minimization — applies backend/db/sql/42_audit_payload_minimization.sql

Entry 11B0. PD-4: `audit.audit_log` was accumulating whole copies of financial
and profile rows in a store with no user foreign key, no cascade and no delete
path.

FUNCTION-ONLY, and deliberately so. No table, column, index, constraint, policy
or grant changes, and no existing audit row is touched — the log is append-only
and this migration does not attempt to make it otherwise. Historical rows are
handled by `scripts/audit_payload_scan.py`, which is a separate, deliberate,
privileged operation rather than something that runs on every deployment.

DOWNGRADE restores 0046's function, which redacts named credential columns but
copies every other value whole. That is the previous behaviour, not a safe one:
downgrading past this revision reinstates PD-4 for writes made afterwards.
Recorded here so the choice is visible rather than discovered.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0048_audit_payload_minimization"
down_revision = "0047_account_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("42_audit_payload_minimization.sql")


def downgrade() -> None:
    # Reinstates the previous audit trigger. See the module docstring: this
    # brings PD-4 back for subsequent writes.
    apply_sql_file("40_audit_secret_redaction.sql")
