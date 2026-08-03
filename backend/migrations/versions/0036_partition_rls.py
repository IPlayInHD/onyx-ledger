"""0036_partition_rls — applies backend/db/sql/30_partition_rls.sql

SECURITY FIX. RLS is not inherited by table partitions, so
finance.income_source_y2025 and its siblings were directly readable across
tenants despite the partitioned parent being protected. Enables and forces RLS
on every existing partition, and installs an event trigger so partitions created
later are secured at creation.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0036_partition_rls"
down_revision = "0035_ioe_outbox_and_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("30_partition_rls.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
