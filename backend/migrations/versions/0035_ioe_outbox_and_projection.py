"""0035_ioe_outbox_and_projection — applies db/sql/29_ioe_outbox_and_projection.sql

P6 closure: a transactional outbox for freshness invalidation, and governed
projection metadata on the rules contract so recurrence is authored rather than
inferred. Additive only.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0035_ioe_outbox_and_projection"
down_revision = "0034_ioe_scenarios"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("29_ioe_outbox_and_projection.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
