"""0040_legacy_integrity_reason — applies backend/db/sql/34_legacy_integrity_reason.sql

Adds `LEGACY_EXECUTION_POLICY_UNVERIFIABLE` to the closed reason enumeration on
`ioe.integrity_check`, so a result sealed before the frozen-input correction is
recorded as `unavailable` with an explicit reason instead of being reported as a
deterministic replay regression it never had the information to be.

Widens one CHECK constraint and adds one index. Every row valid under the old
constraint is valid under the new one, so the rewrite is a validation pass with
no data change.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0040_legacy_integrity_reason"
down_revision = "0039_input_execution_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("34_legacy_integrity_reason.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
