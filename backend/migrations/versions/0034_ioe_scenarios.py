"""0034_ioe_scenarios — applies backend/db/sql/28_ioe_scenarios.sql

P5: pinned scenario specifications, the typed lever/assumption spec tables,
support-score persistence on scenario results, freshness and supersession
state, archive semantics, immutability triggers, and RLS on the new child
tables. Additive only.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0034_ioe_scenarios"
down_revision = "0033_ioe_cost_and_reeval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("28_ioe_scenarios.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
