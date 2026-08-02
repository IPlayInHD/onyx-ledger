"""0030_ioe_portfolio — applies backend/db/sql/24_ioe_portfolio.sql

P4: the commitment taxonomy, pinned portfolio objective with its baseline/final/
delta values and per-concept totals, and the immutable evaluation trace.
Additive only; legacy cost-type values are retained.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0030_ioe_portfolio"
down_revision = "0029_ioe_support_scores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("24_ioe_portfolio.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
