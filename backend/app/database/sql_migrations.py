"""Bridge between Alembic revisions and the canonical SQL schema.

The source of truth for the schema is `backend/db/sql/*.sql`. Each Alembic
revision is a thin wrapper that applies exactly one of those files (in the same
dependency order they run standalone), so `alembic upgrade head` and running the
SQL files by hand produce an identical database. Editing schema going forward
means adding a NEW SQL file + a NEW revision — never mutating an applied one.
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

# backend/app/database/sql_migrations.py -> parents[2] == backend/
SQL_DIR = Path(__file__).resolve().parents[2] / "db" / "sql"

# Bounded contexts (+ shared ref) — dropped as a unit on full teardown.
SCHEMAS = [
    "ref", "identity", "profile", "finance", "wealth", "tax_kb", "rules",
    "analysis", "reco", "ai", "docs", "admin", "billing", "audit",
]
ROLES = ["onyx_app_rw", "onyx_app_ro", "onyx_kb_admin", "onyx_audit_writer", "onyx_migrator"]


def _run_sql(sql: str) -> None:
    """Execute raw multi-statement SQL (incl. dollar-quoted bodies) via the
    DBAPI cursor — bypassing SQLAlchemy's bind-parameter parsing, which would
    otherwise choke on ':' in trigger/DO-block text."""
    raw = op.get_bind().connection  # psycopg2 connection (shares Alembic's tx)
    cur = raw.cursor()
    try:
        cur.execute(sql)
    finally:
        cur.close()


def apply_sql_file(name: str) -> None:
    """Execute a whole SQL file (multi-statement + dollar-quoted bodies) verbatim."""
    _run_sql((SQL_DIR / name).read_text())


def drop_all_schemas() -> None:
    """Full teardown for `alembic downgrade base` (the SQL baseline is a
    forward-only chain applied/removed as one unit)."""
    for schema in SCHEMAS:
        _run_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    for role in ROLES:
        try:
            _run_sql(f'DROP ROLE IF EXISTS "{role}"')
        except Exception:  # noqa: BLE001
            pass
