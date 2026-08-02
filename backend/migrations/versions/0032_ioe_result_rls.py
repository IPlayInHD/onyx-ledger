"""0032_ioe_result_rls — applies backend/db/sql/26_ioe_result_rls.sql

Moves IOE child-evidence tenant isolation from an application invariant into a
database one: every optimization result table resolves ownership back to
`optimization_run.user_id` (or `scenario.user_id`) via RLS. Additive only.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0032_ioe_result_rls"
down_revision = "0031_rules_cost_taxonomy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("26_ioe_result_rls.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
