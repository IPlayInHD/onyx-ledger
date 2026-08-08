"""0047_account_lifecycle — applies backend/db/sql/41_account_lifecycle.sql

Entry 11B1. The durable account-deletion lifecycle: one aggregate row per
account undergoing deletion, an append-only transition log, governed
transitions enforced by trigger, RLS that lets a user start and read their own
deletion but never advance it, and a three-function privileged keyhole for the
cross-tenant worker.

Deletes nothing. Later 11B phases perform the purge this orchestrates.

ADDITIVE. Two new tables, four functions, two triggers, one role. No existing
table, column, constraint, policy or grant is modified.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0047_account_lifecycle"
down_revision = "0046_audit_secret_redaction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("41_account_lifecycle.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
