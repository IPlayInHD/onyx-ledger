"""0038_integrity_verification — applies backend/db/sql/32_integrity_verification.sql

Production replay-integrity verification. Adds the append-only
`ioe.integrity_check` history, current integrity metadata on the three sealed
parents, the scheduling keyhole, and stale-claim recovery.

Additive. The only existing object altered is `ioe.reject_result_mutation()`,
which learns that the four current-integrity-metadata columns on
`strategy_portfolio` are mutable workflow state; every financial and structural
column stays rejected.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0038_integrity_verification"
down_revision = "0037_rel_derivation_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("32_integrity_verification.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
