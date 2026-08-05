"""0039_input_execution_policy — applies backend/db/sql/33_input_execution_policy.sql

Records whether a run was computed under the corrected frozen-snapshot rule or
under the legacy live-source behaviour item 3A removed. Historical rows default
to `live_source_legacy` and are never backfilled as corrected.

Additive: one column, one CHECK, one index. No existing object is altered.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0039_input_execution_policy"
down_revision = "0038_integrity_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("33_input_execution_policy.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
