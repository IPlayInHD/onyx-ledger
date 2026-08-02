"""0029_ioe_support_scores — applies backend/db/sql/23_ioe_support_scores.sql

Independently reviewable: persists the five-stage support-score model and
redefines the pre-existing `confidence_score` column as the derived integer
equivalent of `display_support_score`, with divergence prevented by a trigger
plus a CHECK constraint. Additive only; no dependency on the rest of P3.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0029_ioe_support_scores"
down_revision = "0028_reco_ioe_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("23_ioe_support_scores.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
