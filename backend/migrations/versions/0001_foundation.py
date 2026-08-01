"""0001_foundation — applies backend/db/sql/00_extensions_roles.sql

Baseline of the canonical SQL schema, mirrored one revision per SQL file.
This first revision also owns the FULL teardown (`alembic downgrade base`):
the raw-SQL baseline is a forward-only chain, dropped as a single unit.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file, drop_all_schemas

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("00_extensions_roles.sql")


def downgrade() -> None:
    drop_all_schemas()
