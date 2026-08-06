"""0041_active_calculation_version — applies backend/db/sql/35_active_calculation_version.sql

Entry 9. Makes calculation-version activation an authoritative, atomic,
concurrency-safe database decision instead of an application-startup side
effect. Required because idempotent activation needs persisted state to compare
against; the outbox dedupe key alone could not distinguish "already active"
from "never recorded", and could not be locked.

One table, one unique constraint, one CHECK, one updated-at trigger, two grants.
No user data, so no RLS. Additive: no existing object is altered.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0041_active_calculation_version"
down_revision = "0040_legacy_integrity_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("35_active_calculation_version.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
