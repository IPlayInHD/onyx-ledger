"""0059_subject_severance — applies backend/db/sql/53_subject_severance.sql

Entry 11B6B. Makes subject severance irreversible and puts the whole mechanism
on one subject key.

`identity.deletion_subject` was keyed by `user_id`, so joining it to the
`actor_id` that survives in immutable audit rows restored the severed
correlation in one query. It is now keyed by `subject_key` and carries no
account id at all. It also had its own DEFAULT, minting a key unrelated to the
one `identity.account_subject` had already issued, so audit rows and login
events carried different keys for the same person.

DOWNGRADE re-applies 50_audit_auth_deidentification.sql, restoring the
reversible shape. Development rollback only — see that file's own header.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0059_subject_severance"
down_revision = "0058_audit_writer_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("53_subject_severance.sql")


def downgrade() -> None:
    apply_sql_file("50_audit_auth_deidentification.sql")
