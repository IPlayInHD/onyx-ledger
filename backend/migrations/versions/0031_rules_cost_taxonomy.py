"""0031_rules_cost_taxonomy — applies backend/db/sql/25_rules_cost_taxonomy.sql

Widens the rule-authored cost_type CHECK to the separated P4 commitment
taxonomy so rule data can express liquidity commitments, asset transfers, and
nonrecoverable expenditure distinctly. Additive only; legacy values retained.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0031_rules_cost_taxonomy"
down_revision = "0030_ioe_portfolio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("25_rules_cost_taxonomy.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
