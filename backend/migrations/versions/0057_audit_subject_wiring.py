"""0057_audit_subject_wiring — applies backend/db/sql/51_audit_subject_wiring.sql

Entry 11B6A. Adds `identity.subject_key_for`, which mints and returns the
subject key for a LIVE account and returns NULL for one that no longer exists —
so a later audit write can never re-attribute a subject that has already been
de-identified.

Wiring `audit.log_change` to call it is NOT in this revision; see the SQL
file's closing note for why, and for what remains.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0057_audit_subject_wiring"
down_revision = "0056_audit_auth_deident"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("51_audit_subject_wiring.sql")


def downgrade() -> None:
    from alembic import op
    op.execute("DROP FUNCTION IF EXISTS identity.subject_key_for(uuid)")
