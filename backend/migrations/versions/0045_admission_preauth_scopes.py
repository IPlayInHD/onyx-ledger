"""0045_admission_preauth_scopes — applies backend/db/sql/39_admission_preauth_scopes.sql

Entry 10 Phase 2. Widens `admission.rate_counter`'s scope CHECK to admit the two
pre-authentication scopes the login throttle needs (IP, AUTH_SUBJECT).

`admission.lease` is deliberately NOT widened — see the SQL file for why.

CONSTRAINT-ONLY. No column, index, grant, policy or row is touched, and every
existing row already satisfies the wider predicate.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0045_admission_preauth_scopes"
down_revision = "0044_admission_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("39_admission_preauth_scopes.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
