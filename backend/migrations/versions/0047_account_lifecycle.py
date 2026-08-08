"""0047_account_lifecycle — applies backend/db/sql/41_account_lifecycle.sql

Entry 11B1. The durable account-deletion lifecycle: one aggregate row per
account undergoing deletion, an append-only transition log, governed
transitions enforced by trigger, RLS that lets a user start and read their own
deletion but never advance it, and a three-function privileged keyhole for the
cross-tenant worker.

Deletes nothing. Later 11B phases perform the purge this orchestrates.

ADDITIVE. Two new tables, their triggers and policies, the privileged worker
role and its functions. No existing table, column, constraint, policy or grant
is modified — with one deliberate exception: the grants this file issues on its
own two tables REVOKE first, because 16_rls_grants.sql sets ALTER DEFAULT
PRIVILEGES granting UPDATE and DELETE on every new table in the identity
schema, and a lifecycle row an ordinary role can update is not a lifecycle.
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
