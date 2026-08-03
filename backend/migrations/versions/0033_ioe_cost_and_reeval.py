"""0033_ioe_cost_and_reeval — applies backend/db/sql/27_ioe_cost_and_reeval.sql

Records how each candidate cost was resolved into the P4 commitment taxonomy
(keeping the rule-authored value verbatim), and adds the explicit
`requires_re_evaluation` state for candidates whose eligibility facts an earlier
portfolio action changed. Additive only.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0033_ioe_cost_and_reeval"
down_revision = "0032_ioe_result_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("27_ioe_cost_and_reeval.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
