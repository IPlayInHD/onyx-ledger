"""0026_rules_contract — applies backend/db/sql/20_rules_contract.sql

Phase P0 of the IOE plan: the RulesEvaluator Opportunity contract v2. Additive
only, and deliberately FIRST in the IOE migration order — it has no dependency
on the `ioe` schema, so contract emission can ship and be exercised before any
IOE table exists (Revision 2.1 §F).
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0026_rules_contract"
down_revision = "0025_seed_tkms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("20_rules_contract.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
