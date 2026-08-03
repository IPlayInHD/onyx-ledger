"""0037_rel_derivation_source — applies backend/db/sql/31_relationship_derivation_source.sql

Adds `ioe.recommendation_relationship.derivation_source` so every relationship
edge records why it exists. Required by the sparse derivation, which trims only
derived edges and never an authoritative rules-supplied one.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0037_rel_derivation_source"
down_revision = "0036_partition_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("31_relationship_derivation_source.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
