"""0042_freshness_scope_and_reasons — applies backend/db/sql/36_freshness_scope_and_reasons.sql

Entry 9 remaining domains. Adds a jurisdiction QUALIFIER to the freshness outbox
so a rule published for one province stops staling every other province's
results, and adds `NEWER_ANALYSIS_AVAILABLE` so analysis supersession stops
borrowing the reason that means "your financial data changed". Three registry
event types are admitted so activation can name what moved.

Additive: one nullable column, one partial index, two widened CHECK
constraints. Every existing row satisfies both, so the rewrites validate without
changing data, and NULL jurisdiction preserves the historical fan-out exactly.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0042_freshness_scope_and_reasons"
down_revision = "0041_active_calculation_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("36_freshness_scope_and_reasons.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
