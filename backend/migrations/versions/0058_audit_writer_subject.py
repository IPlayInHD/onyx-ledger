"""0058_audit_writer_subject — applies backend/db/sql/52_audit_log_change_subject.sql

Entry 11B6B. Makes PD-15's subject column load-bearing: `audit.log_change`
resolves the account a change CONCERNED and writes it into the immutable audit
row at insert time, alongside — not instead of — the actor.

A NEW canonical file rather than an edit to 42_audit_payload_minimization.sql,
because `apply_sql_file` reads its file at migration run time and revision 0048
applies 42: editing it would change what 0048 installs on a fresh database.

DOWNGRADE re-applies 42, restoring the pre-subject writer. Audit rows already
written keep their subject_key — the audit log is append-only and nothing is
rewritten — so a rollback stops recording new subjects rather than erasing old
ones.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0058_audit_writer_subject"
down_revision = "0057_audit_subject_wiring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("52_audit_log_change_subject.sql")


def downgrade() -> None:
    apply_sql_file("42_audit_payload_minimization.sql")
